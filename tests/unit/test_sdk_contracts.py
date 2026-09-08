"""SDK public methods, wire encoding and failure/retry contracts."""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from pulse.client import PulseClient, PulseError, PulseValidationError, PulseNotFoundError, PulseThrottlingError, _SigV4Auth


def response(code, data=None):
    result = MagicMock(status_code=code, text='response')
    result.json.return_value = data if data is not None else {'error':'test'}
    return result


def test_sdk_public_methods_sign_requests_and_keep_publish_key_on_retry():
    client = PulseClient('https://example.test', max_retries=1)
    with patch.object(client._session, 'request') as send, patch('pulse.client.time.sleep'):
        send.side_effect = [response(503), response(201, {'signalId':'s', 'status':'new'})]
        assert client.publish_signal('app','incident',{'level':'low'}, {'title':'test'}, context={'account_id':'a'}, audience_hint={'personas':['p']}, correlation={'time_window_seconds':30}).signal_id == 's'
        calls = send.call_args_list
        assert calls[0].kwargs['headers']['Idempotency-Key'] == calls[1].kwargs['headers']['Idempotency-Key']
        send.side_effect = None
        send.return_value = response(201, {'personaId':'p'})
        assert client.create_persona('P', 'sre', [{'principalId':'a','channels':['email']}], delivery_preferences={'channels':['email']}).persona_id == 'p'
        assert client.update_persona('p/id', name='New').persona_id == 'p'
        assert '/p%2Fid' in send.call_args.kwargs['url']
        send.return_value = response(200, {'signalId':'s'})
        assert client.get_signal('s').signal_id == 's'
        send.return_value = response(200, {'deliveries':[]})
        client.list_deliveries(signal_id='s', persona_id='p')
        assert 'signalId=s' in send.call_args.kwargs['url'] and 'personaId=p' in send.call_args.kwargs['url']
        client.submit_feedback('d','resolved')
        assert json.loads(send.call_args.kwargs['data'])['feedback'] == 'resolved'
        client.subscribe('p/id', 'RDS failures')
        assert '/p%2Fid/subscribe' in send.call_args.kwargs['url']
        client.get_nrs()
        assert send.call_args.kwargs['url'].endswith('/v1/analytics/nrs')
    with pytest.raises(PulseValidationError):
        client.submit_feedback('d', 'invalid')


@pytest.mark.parametrize('kwargs', [{'endpoint_url':''}, {'endpoint_url':'https://test','max_retries':-1}, {'endpoint_url':'https://test','timeout':0}])
def test_sdk_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        PulseClient(**kwargs)


def test_signing_requires_credentials():
    with patch('pulse.client.botocore.session.get_session') as session:
        session.return_value.get_credentials.return_value = None
        with pytest.raises(ValueError):
            _SigV4Auth('us-east-1')


@pytest.mark.parametrize('status,exception', [(400,PulseValidationError),(404,PulseNotFoundError),(429,PulseThrottlingError),(403,PulseError),(500,PulseError)])
def test_sdk_error_mapping(status, exception):
    client = PulseClient('https://test', max_retries=0)
    with pytest.raises(exception) as caught:
        client._handle_response(response(status))
    assert caught.value.status_code == status
    with patch.object(client._session, 'request', return_value=response(status)):
        with pytest.raises(exception):
            client.get_nrs()


@pytest.mark.parametrize('failure', [requests.Timeout('timeout'), requests.ConnectionError('connection')])
def test_sdk_network_retry_exhaustion(failure):
    client = PulseClient('https://test', max_retries=1)
    with patch.object(client._session, 'request', side_effect=failure) as send, patch('pulse.client.time.sleep') as sleep:
        with pytest.raises(PulseError):
            client.get_nrs()
        assert send.call_count == 2 and sleep.call_count == 1


def test_sdk_throttle_retry_and_non_json_error():
    client = PulseClient('https://test', max_retries=1)
    with patch.object(client._session, 'request', side_effect=[response(429), response(200, {})]), patch('pulse.client.time.sleep'):
        assert client.get_nrs() == {}
    bad = response(502)
    bad.json.side_effect = ValueError()
    with pytest.raises(PulseError) as caught:
        client._handle_response(bad)
    assert caught.value.response_body == {'raw':'response'}


def test_sdk_does_not_repeat_non_idempotent_post_after_ambiguous_failure():
    client = PulseClient('https://test', max_retries=3)
    for result in [response(503), requests.Timeout('response lost')]:
        with patch.object(client._session, 'request') as send, patch('pulse.client.time.sleep') as sleep:
            if isinstance(result, Exception):
                send.side_effect = result
            else:
                send.return_value = result
            with pytest.raises(PulseError):
                client.create_persona('P', 'sre', [{'principalId':'a','channels':['email']}])
            send.assert_called_once()
            sleep.assert_not_called()

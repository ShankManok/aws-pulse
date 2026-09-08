"""Audit event retention and exact daily metrics on real emulated service schemas."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws
from botocore.exceptions import ClientError
from boto3.dynamodb.types import TypeSerializer
from src.learning import audit_exporter, nrs_calculator


def test_audit_snapshots_and_mutations_are_append_only(monkeypatch):
    with mock_aws():
        s3 = boto3.client('s3')
        s3.create_bucket(Bucket='pulse-audit-test', ObjectLockEnabledForBucket=True)
        s3.put_object_lock_configuration(Bucket='pulse-audit-test', ObjectLockConfiguration={'ObjectLockEnabled':'Enabled','Rule':{'DefaultRetention':{'Mode':'COMPLIANCE','Days':365}}})
        ddb = boto3.resource('dynamodb')
        table = ddb.create_table(TableName='deliveries',KeySchema=[{'AttributeName':'deliveryId','KeyType':'HASH'}],AttributeDefinitions=[{'AttributeName':'deliveryId','AttributeType':'S'}],BillingMode='PAY_PER_REQUEST')
        monkeypatch.setenv('AUDIT_BUCKET_NAME','pulse-audit-test')
        monkeypatch.setenv('DELIVERY_TABLE_NAME','deliveries')
        fixed = datetime(2026,1,2,12,tzinfo=timezone.utc)
        with patch.object(audit_exporter,'s3',s3), patch.object(audit_exporter,'dynamodb',ddb), patch.object(audit_exporter,'now',return_value=fixed):
            assert audit_exporter.handler({},None)['exported'] == 0
            row = {'deliveryId':'d','orgId':'default','deliveredAt':'2026-01-02T11:01:00Z','actionTokenHash':'private','value':Decimal('0.5')}
            table.put_item(Item=row)
            table.put_item(Item={**row,'deliveryId':'excluded','deliveredAt':'2026-01-02T12:00:00Z'})
            first = audit_exporter.handler({},None)
            assert first['exported'] == 1
            assert audit_exporter.handler({},None)['s3_key'] == first['s3_key']
            obj = s3.get_object(Bucket='pulse-audit-test',Key=first['s3_key'])
            data = json.loads(obj['Body'].read())
            assert data['value'] == .5 and 'actionTokenHash' not in data
            assert obj['ObjectLockMode'] == 'COMPLIANCE'
            def record(id, kind, image):
                return {'eventID':id,'eventName':kind,'dynamodb':{'SequenceNumber':id,'ApproximateCreationDateTime':123,'OldImage' if kind=='REMOVE' else 'NewImage':{k:TypeSerializer().serialize(v) for k,v in image.items()}}}
            events = [record('1','INSERT',row),record('2','MODIFY',{**row,'feedback':'useful'}),record('3','REMOVE',row)]
            assert audit_exporter.handler({'Records':events},None)['batchItemFailures'] == []
            assert audit_exporter.handler({'Records':events},None)['batchItemFailures'] == []
            assert s3.list_objects_v2(Bucket='pulse-audit-test')['KeyCount'] == 4
            assert audit_exporter.handler({'Records':[record('4','MODIFY',{**row,'orgId':'other'})]},None)['batchItemFailures'] == [{'itemIdentifier':'4'}]
            with patch.object(s3,'put_object',side_effect=ClientError({'Error':{'Code':'AccessDenied'}},'PutObject')):
                with pytest.raises(ClientError):
                    audit_exporter.handler({},None)
        monkeypatch.setenv('AUDIT_BUCKET_NAME','')
        with pytest.raises(ValueError):
            audit_exporter.handler({},None)


def test_audit_pagination_does_not_drop_pages_or_other_tenants():
    table = MagicMock()
    table.scan.side_effect = [{'Items':[{'orgId':'default'}],'LastEvaluatedKey':{'deliveryId':'1'}},{'Items':[{'orgId':'other'},{'orgId':'default'}]}]
    assert len(audit_exporter._scan_deliveries(table,'start','end')) == 2
    assert table.scan.call_args.kwargs['ExclusiveStartKey'] == {'deliveryId':'1'}
    table.scan.side_effect = RuntimeError('read failure')
    with pytest.raises(RuntimeError):
        audit_exporter._scan_deliveries(table,'start','end')


def test_nrs_excludes_receipts_correlation_future_and_other_tenants(monkeypatch):
    with mock_aws():
        table = boto3.resource('dynamodb').create_table(TableName='signals',KeySchema=[{'AttributeName':'signalId','KeyType':'HASH'}],AttributeDefinitions=[{'AttributeName':'signalId','AttributeType':'S'}],BillingMode='PAY_PER_REQUEST')
        for i,status in enumerate(['correlated','deduplicated','suppressed','new']):
            table.put_item(Item={'signalId':str(i),'ingestedAt':'2026-01-01T12:00:00Z','status':status,'org_id':'default','recordType':'signal'})
        table.put_item(Item={'signalId':'receipt','ingestedAt':'RECEIPT','recordType':'receipt'})
        table.put_item(Item={'signalId':'future','ingestedAt':'2026-01-02T00:00:00Z'})
        table.put_item(Item={'signalId':'other','ingestedAt':'2026-01-01T12:00:00Z','org_id':'other'})
        assert nrs_calculator._count_signals_by_status(table,'2026-01-01',None,'2026-01-02') == 4
        assert nrs_calculator._count_signals_by_status(table,'2026-01-01','deduplicated','2026-01-02') == 1


def test_analytics_failures_retry_and_malformed_ack_times_are_ignored():
    table = MagicMock()
    table.scan.side_effect = [{'Items':[{'deliveredAt':'2026-01-01T10:00:00Z','acknowledgedAt':'2026-01-01T10:05:00+00:00'}, {'deliveredAt':'bad','acknowledgedAt':'bad'}],'LastEvaluatedKey':{'id':'1'}},{'Items':[{'deliveredAt':'2026-01-01T11:00:00Z','acknowledgedAt':'2026-01-01T10:00:00Z'}]}]
    assert nrs_calculator._compute_mtta(table,'2026-01-01') == 300
    with patch.object(nrs_calculator,'cloudwatch') as cw:
        cw.put_metric_data.side_effect = RuntimeError('write failed')
        with pytest.raises(RuntimeError):
            nrs_calculator._publish_metrics('default','test',0,0,0,0)
    table.scan.side_effect = None
    table.scan.return_value = {'Count':0,'Items':[]}
    table.put_item.side_effect = RuntimeError('storage failed')
    with patch.object(nrs_calculator,'dynamodb') as ddb, patch.object(nrs_calculator,'cloudwatch'):
        ddb.Table.return_value = table
        with pytest.raises(RuntimeError):
            nrs_calculator.handler({},None)

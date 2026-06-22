"""Drop legacy external_device tables after app removal."""

from django.db import migrations

_DROP_TABLES = """
DROP TABLE IF EXISTS external_device_devicehealthtelemetry;
DROP TABLE IF EXISTS external_device_mqttpublishoutbox;
DROP TABLE IF EXISTS external_device_smsdispatchoutbox;
DROP TABLE IF EXISTS external_device_mqttgatewaycatalogentry;
DROP TABLE IF EXISTS external_device_smsrecipientstatus;
DROP TABLE IF EXISTS external_device_inboxmessage;
DROP TABLE IF EXISTS external_device_smsrequest;
DROP TABLE IF EXISTS external_device_devicesession;
DROP TABLE IF EXISTS external_device_hidishelinkdevice;
DROP TABLE IF EXISTS external_device_externaldevice;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('sms', '0003_inboundwebhook'),
    ]

    operations = [
        migrations.RunSQL(_DROP_TABLES, reverse_sql=migrations.RunSQL.noop),
    ]

"""Performance edge: sanitized_device_id, dispatch/mqtt outbox, recipient pending."""

from django.db import migrations, models


def populate_sanitized_device_ids(apps, schema_editor):
    ExternalDevice = apps.get_model('external_device', 'ExternalDevice')
    for device in ExternalDevice.objects.all():
        device_id = device.device_id or ''
        sanitized = device_id.replace('+', '').replace('#', '')
        ExternalDevice.objects.filter(pk=device.pk).update(sanitized_device_id=sanitized or device_id)


class Migration(migrations.Migration):

    dependencies = [
        ('external_device', '0004_device_session_health_telemetry'),
    ]

    operations = [
        migrations.AddField(
            model_name='externaldevice',
            name='sanitized_device_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='MQTT topic-safe device id (derived from device_id)',
                max_length=64,
                null=True,
                unique=True,
            ),
        ),
        migrations.RunPython(populate_sanitized_device_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='externaldevice',
            name='sanitized_device_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='MQTT topic-safe device id (derived from device_id)',
                max_length=64,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name='smsrecipientstatus',
            name='status',
            field=models.CharField(
                choices=[('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed')],
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name='SmsDispatchOutbox',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('job_type', models.CharField(choices=[('sms_request', 'SMS request'), ('outbound', 'Outbound SMS pk'), ('remote', 'Remote hiDisheLink send')], db_index=True, max_length=32)),
                ('reference', models.CharField(db_index=True, max_length=64)),
                ('priority', models.CharField(default='normal', max_length=16)),
                ('payload_json', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('dispatched_at', models.DateTimeField(blank=True, db_index=True, null=True)),
            ],
            options={
                'ordering': ['created_at'],
                'indexes': [models.Index(fields=['dispatched_at', 'created_at'], name='external_de_dispatc_6a0f2d_idx')],
            },
        ),
        migrations.CreateModel(
            name='MqttPublishOutbox',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('topic', models.CharField(db_index=True, max_length=512)),
                ('payload_json', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('published_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('retry_count', models.PositiveSmallIntegerField(default=0)),
            ],
            options={
                'ordering': ['created_at'],
                'indexes': [models.Index(fields=['published_at', 'created_at'], name='external_de_publish_8c4e1a_idx')],
            },
        ),
    ]

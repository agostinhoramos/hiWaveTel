# Generated manually for HiDisheLink admin integration device model.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('external_device', '0002_mqtt_gateway_catalog'),
    ]

    operations = [
        migrations.CreateModel(
            name='HiDishelinkDevice',
            fields=[
                ('device_id', models.CharField(help_text='Device identifier on hiDisheLink (e.g. E.164 +351913000388)', max_length=64, primary_key=True, serialize=False)),
                ('api_url', models.CharField(default='http://192.168.1.77:5201', help_text='Base URL only (no trailing path), e.g. http://192.168.1.77:5201', max_length=512)),
                ('api_key', models.TextField(blank=True, help_text='Plaintext api_key from hiDisheLink (admin-only; store securely)')),
                ('registration_token', models.CharField(blank=True, help_text='Optional one-time registration token for POST /device/register/', max_length=512)),
                ('session_id', models.CharField(blank=True, max_length=512)),
                ('session_expires_at', models.DateTimeField(blank=True, null=True)),
                ('mqtt_config', models.JSONField(blank=True, help_text='Last mqtt-config payload (flat dict with MQTT_* and TOPIC_* keys)', null=True)),
                ('mqtt_config_fetched_at', models.DateTimeField(blank=True, null=True)),
                ('status', models.CharField(choices=[('unconfigured', 'Unconfigured'), ('registered', 'Registered'), ('active', 'Active'), ('error', 'Error')], db_index=True, default='unconfigured', max_length=16)),
                ('last_seen', models.DateTimeField(blank=True, null=True)),
                ('last_api_error', models.TextField(blank=True)),
                ('sync_external_device', models.BooleanField(default=True, help_text='If true, ensure an ExternalDevice row exists for MQTT inbox bridging')),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'HiDisheLink device',
                'verbose_name_plural': 'HiDisheLink devices',
                'ordering': ['-updated_at'],
            },
        ),
    ]

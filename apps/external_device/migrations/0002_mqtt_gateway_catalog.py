# Generated manually for MQTT modem catalog parity (snapshot / contacts).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('external_device', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='MqttGatewayCatalogEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('snapshot', 'Snapshot'), ('contacts', 'Contacts')], db_index=True, max_length=16)),
                ('payload', models.JSONField(help_text='Raw JSON body from modems/snapshot or modems/contacts')),
                ('received_at', models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                'verbose_name': 'MQTT modem catalog entry',
                'verbose_name_plural': 'MQTT modem catalog entries',
                'ordering': ['-received_at'],
            },
        ),
    ]

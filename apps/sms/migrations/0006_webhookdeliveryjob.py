from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('sms', '0005_modemdevice_inboundwebhook_modem_index'),
    ]

    operations = [
        migrations.CreateModel(
            name='WebhookDeliveryJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('inbound', 'Inbound'), ('outbound', 'Outbound')], db_index=True, max_length=16)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('processing', 'Processing'), ('delivered', 'Delivered'), ('failed', 'Failed')], db_index=True, default='pending', max_length=16)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('last_error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('delivered_at', models.DateTimeField(blank=True, null=True)),
                ('inbound_sms', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='webhook_job', to='sms.inboundsms')),
                ('outbound_sms', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='webhook_job', to='sms.outboundsms')),
            ],
            options={
                'ordering': ['created_at'],
                'indexes': [models.Index(fields=['status', 'created_at'], name='sms_webhook_status_created_idx')],
            },
        ),
    ]

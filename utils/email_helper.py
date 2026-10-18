from email.message import EmailMessage
import smtplib

from flask import current_app


def send_password_reset_email(to_email, reset_url):
    settings = {
        key: current_app.config.get(key)
        for key in (
            'SMTP_HOST',
            'SMTP_PORT',
            'SMTP_USERNAME',
            'SMTP_PASSWORD',
            'SMTP_FROM_EMAIL',
            'PUBLIC_BASE_URL',
        )
    }
    if not all(settings.values()):
        raise RuntimeError('Password reset email is not configured.')

    message = EmailMessage()
    message['Subject'] = 'CarePoint password reset'
    message['From'] = settings['SMTP_FROM_EMAIL']
    message['To'] = to_email
    message.set_content(
        'A password reset was requested for your CarePoint account.\n\n'
        f'Use this single-use link within 30 minutes:\n{reset_url}\n\n'
        'If you did not request this, you can ignore this message.'
    )

    smtp_host = settings['SMTP_HOST']
    smtp_port = int(settings['SMTP_PORT'])
    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(settings['SMTP_USERNAME'], settings['SMTP_PASSWORD'])
        refused = server.send_message(message)
        if refused:
            raise RuntimeError('Password reset email recipient was refused.')

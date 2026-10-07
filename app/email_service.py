import os
import smtplib
from dotenv import load_dotenv
from email.message import EmailMessage

load_dotenv()

class EmailService:
    def __init__(self):
        self.sender = os.getenv("EMAIL")
        self.app_password = os.getenv("APP_PASSWORD")

    def send_email(self, recipient: str, subject: str, body: str):
        msg = EmailMessage()
        msg.set_content(body)
        msg['Subject'] = subject
        msg['From'] = self.sender
        msg['To'] = recipient

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(self.sender, self.app_password)
            smtp.send_message(msg)
        print("Email sent successfully!")




import tempfile
import unittest
from unittest.mock import patch

from app import db, sms


class OTPTest(unittest.TestCase):
    def test_login_and_failure_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = db.DB_PATH
            db.DB_PATH = tmp + '/test.db'
            try:
                from app.main import app, limiter
                from fastapi.testclient import TestClient
                db.init()
                limiter.reset()
                sms.OTP_CACHE.clear()
                sms.OTP_ATTEMPTS.clear()
                phone = '09123456789'
                with TestClient(app) as client, patch('app.main.send_sms_melipayamak', return_value=True):
                    self.assertEqual(client.post('/api/auth/send-otp', json={'phone': 'bad'}).status_code, 400)
                    self.assertEqual(client.post('/api/auth/send-otp', json={'phone': phone}).status_code, 200)
                    code = sms.OTP_CACHE[phone][0]
                    self.assertEqual(client.post('/api/auth/send-otp', json={'phone': phone}).status_code, 429)
                    result = client.post('/api/auth/verify-otp', json={'phone': phone, 'code': code})
                    self.assertEqual(result.status_code, 200)
                    self.assertIn('session', result.cookies)
                    self.assertEqual(client.get('/api/me').json()['phone'], phone)
                    self.assertEqual(client.post('/api/auth/verify-otp', json={'phone': phone, 'code': code}).status_code, 400)
                    limiter.reset()
                    with patch('app.main.send_sms_melipayamak', return_value=False):
                        self.assertEqual(client.post('/api/auth/send-otp', json={'phone': phone}).status_code, 502)
                    self.assertNotIn(phone, sms.OTP_CACHE)
                    self.assertFalse(sms.verify_otp_code(phone, '1111'))
            finally:
                db.DB_PATH = original
                sms.OTP_CACHE.clear()
                sms.OTP_ATTEMPTS.clear()

    def test_expiry_and_attempt_limit(self):
        phone = '09120000000'
        try:
            code = sms.generate_otp(phone)
            for _ in range(5):
                self.assertFalse(sms.verify_otp_code(phone, '0000'))
            self.assertFalse(sms.verify_otp_code(phone, code))
            sms.OTP_CACHE[phone] = (code, 0)
            self.assertFalse(sms.verify_otp_code(phone, code))
        finally:
            sms.OTP_CACHE.pop(phone, None)
            sms.OTP_ATTEMPTS.pop(phone, None)

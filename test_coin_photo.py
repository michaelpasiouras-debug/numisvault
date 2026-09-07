import base64
import io
import os
import unittest
from unittest.mock import patch, Mock
from flask import Flask
from PIL import Image
from coin_photo import photo_api


class PhotoTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(photo_api)
        self.client = app.test_client()
        buf = io.BytesIO()
        Image.new('RGB', (20, 20)).save(buf, format='PNG')
        self.image = {'mime_type': 'image/png', 'image_data': base64.b64encode(buf.getvalue()).decode()}
        self.env = patch.dict(os.environ, {'NUMISTA_IMAGE_SEARCH_ENABLED':'true', 'NUMISTA_API_KEY':'test'})
        self.env.start()
        self.addCleanup(self.env.stop)

    @patch('coin_photo.requests.post')
    def test_two_sides_one_call_and_review(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {'types':[{'id':420, 'title':'5 Cents', 'issuer':{'name':'Canada'}}]}
        r = self.client.post('/api/coin-identify-images', json={'images':[self.image, self.image]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json['status'], 'review')
        post.assert_called_once()
        self.assertEqual(len(post.call_args.kwargs['json']['images']), 2)
        self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.assertNotIn('image_data', r.get_data(as_text=True))

    @patch('coin_photo.requests.post')
    def test_invalid_uploads_make_no_paid_call(self, post):
        for images in [[self.image], [self.image, {'mime_type':'image/png','image_data':'bogus'}]]:
            self.assertEqual(self.client.post('/api/coin-identify-images', json={'images':images}).status_code, 400)
        post.assert_not_called()

    @patch('coin_photo.requests.post')
    def test_disabled_makes_no_call(self, post):
        with patch.dict(os.environ, {'NUMISTA_IMAGE_SEARCH_ENABLED':'false'}):
            self.assertEqual(self.client.post('/api/coin-identify-images', json={}).status_code, 503)
        post.assert_not_called()

    @patch('coin_photo.requests.post')
    def test_permission_failure_is_explicit(self, post):
        post.return_value = Mock(status_code=403)
        r = self.client.post('/api/coin-identify-images', json={'images':[self.image, self.image]})
        self.assertEqual(r.status_code, 503)
        self.assertIn('not activated', r.json['error'])


if __name__ == '__main__':
    unittest.main()

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.grok.utils.response import wrap_image_content


class WrapImageContentTests(unittest.TestCase):
    def test_base64_format_url_content_should_not_be_wrapped_as_data_uri(self):
        output = wrap_image_content("https://example.com/image.jpg", "b64_json")
        self.assertEqual(output, "![image](https://example.com/image.jpg)")

    def test_base64_format_raw_base64_should_be_wrapped_as_data_uri(self):
        output = wrap_image_content("aGVsbG8=", "b64_json")
        self.assertEqual(output, "![image](data:image/png;base64,aGVsbG8=)")

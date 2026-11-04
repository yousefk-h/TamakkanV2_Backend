"""Profile selection must not inherit secrets from the original .env file."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.core.config import get_settings


class EnvironmentProfileTests(unittest.TestCase):
    def test_explicit_profile_does_not_load_default_dotenv(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            try:
                os.chdir(directory)
                Path('.env').write_text('MONGODB_DB_NAME=default_profile\nGEMINI_API_KEY=default-profile-only\n')
                Path('profile.env').write_text('MONGODB_DB_NAME=tamakkan_dev\nJWT_SECRET_KEY=tamakkan-test-key\n')
                os.environ['TAMAKKAN_ENV_FILE'] = 'profile.env'
                get_settings.cache_clear()
                settings = get_settings()
                self.assertEqual(settings.MONGODB_DB_NAME, 'tamakkan_dev')
                self.assertEqual(settings.JWT_SECRET_KEY, 'tamakkan-test-key')
                self.assertEqual(settings.GEMINI_API_KEY, '')
            finally:
                get_settings.cache_clear()
                os.chdir(original)

    def test_default_profile_reads_default_dotenv(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            try:
                os.chdir(directory)
                Path('.env').write_text('MONGODB_DB_NAME=default_existing\n')
                get_settings.cache_clear()
                self.assertEqual(get_settings().MONGODB_DB_NAME, 'default_existing')
            finally:
                get_settings.cache_clear()
                os.chdir(original)

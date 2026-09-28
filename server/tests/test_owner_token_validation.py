
import os
import secrets
import subprocess
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.core.config import Settings


class OwnerTokenValidationTest(unittest.TestCase):
    def test_historical_placeholder_rejected_in_settings(self):
        placeholder = "replace_with_random_token_at_least_32_chars"
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                owner_token=placeholder,
                env="test",
                auth_mode="session",
            )
        self.assertIn("cannot use the default placeholder token", str(ctx.exception))

    def test_historical_placeholder_rejected_even_in_dev_bypass(self):
        placeholder = "replace_with_random_token_at_least_32_chars"
        with self.assertRaises(ValidationError) as ctx:
            Settings(
                owner_token=placeholder,
                env="development",
                auth_mode="dev_bypass",
            )
        self.assertIn("cannot use the default placeholder token", str(ctx.exception))

    def test_high_entropy_random_token_passes(self):
        strong_token = secrets.token_urlsafe(32)
        settings = Settings(
            owner_token=strong_token,
            env="test",
            auth_mode="session",
        )
        self.assertEqual(settings.owner_token.get_secret_value(), strong_token)

    def test_compose_config_requires_owner_token_env(self):
        repo_root = Path(__file__).resolve().parents[2]
        compose_file = repo_root / "docker-compose.yml"
        if not compose_file.is_file():
            self.skipTest("docker-compose.yml not found")

        env_without = dict(os.environ)
        env_without.pop("CCM_OWNER_TOKEN", None)
        proc = subprocess.run(
            ["docker", "compose", "config"],
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            env=env_without,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CCM_OWNER_TOKEN must be set", proc.stderr + proc.stdout)

        env_with = dict(os.environ)
        env_with["CCM_OWNER_TOKEN"] = secrets.token_urlsafe(32)
        proc_ok = subprocess.run(
            ["docker", "compose", "config"],
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            env=env_with,
        )
        self.assertEqual(proc_ok.returncode, 0, proc_ok.stderr)


if __name__ == "__main__":
    unittest.main()

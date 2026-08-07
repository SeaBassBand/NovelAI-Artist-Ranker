#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from generation_profiles import (  # noqa: E402
    OWNERSHIP_FULL,
    OWNERSHIP_PROMPT,
    PROFILE_SCHEMA_VERSION,
    apply_profile,
    build_profile,
    default_generation_settings,
    normalize_profile,
    profile_summary,
    snapshot_profile,
)


class GenerationProfileCfgTests(unittest.TestCase):
    def setUp(self):
        self.defaults = default_generation_settings(
            width=832,
            height=1216,
            steps=10,
            sampler="k_euler_ancestral",
            cfg_scale=1.0,
        )

    def _profile(self, ownership=OWNERSHIP_FULL, cfg_scale=7.34):
        return build_profile(
            ownership=ownership,
            positive="portrait",
            negative="blurry",
            resolution_preset="portrait",
            width=832,
            height=1216,
            steps=22,
            cfg_scale=cfg_scale,
            sampler="k_euler_ancestral",
            scheduler="default",
            uc_preset=0,
            quality_toggle=True,
            decrisp_mode=False,
            variety_boost=False,
            notes="cfg test",
            defaults=self.defaults,
        )

    def test_old_profile_migrates_to_backend_default_cfg(self):
        profile, migrated = normalize_profile(
            {"schema_version": 2, "ownership": "full", "positive": "old"},
            self.defaults,
        )
        self.assertTrue(migrated)
        self.assertEqual(profile["schema_version"], PROFILE_SCHEMA_VERSION)
        self.assertEqual(profile["cfg_scale"], 1.0)

    def test_complete_profile_rounds_applies_and_snapshots_cfg(self):
        profile = self._profile()
        self.assertEqual(profile["cfg_scale"], 7.3)
        applied = apply_profile(profile, {**self.defaults, "cfg_scale": 2.0}, self.defaults)
        self.assertEqual(applied["cfg_scale"], 7.3)
        snapshot = snapshot_profile("Test", profile, applied, self.defaults)
        self.assertEqual(snapshot["cfg_scale"], 7.3)
        self.assertIn("CFG 7.3", profile_summary("Test", profile, self.defaults))

    def test_prompt_only_profile_does_not_replace_current_cfg(self):
        profile = self._profile(ownership=OWNERSHIP_PROMPT, cfg_scale=9.0)
        applied = apply_profile(profile, {**self.defaults, "cfg_scale": 2.5}, self.defaults)
        self.assertEqual(applied["cfg_scale"], 2.5)


if __name__ == "__main__":
    unittest.main()

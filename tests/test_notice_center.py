import hashlib
import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class NoticeCenterTests(unittest.TestCase):
    def test_notice_manifest_includes_fluxion_tutorial(self) -> None:
        manifest = json.loads((REPO_ROOT / "docs" / "tutorial-announcements.json").read_text(encoding="utf-8"))
        items = manifest["items"]

        self.assertEqual(
            [item["id"] for item in items],
            ["provider-co-creation-invite", "project-learning-ai-prompt", "fluxion-ai-getting-started"],
        )
        self.assertTrue(items[0]["pinned"])
        self.assertEqual(items[0]["type"], "announcement")
        self.assertEqual(items[1]["type"], "announcement")
        self.assertEqual(items[1]["badge"], "AI 学习")
        self.assertEqual(items[2]["type"], "tutorial")
        self.assertEqual(items[2]["title"], "Fluxion AI 新手引导")
        self.assertEqual(items[2]["date"], "2026-08-29")
        self.assertEqual(items[2]["path"], "docs/tutorials/fluxion-ai-getting-started.md")

    def test_fluxion_tutorial_keeps_expected_structure_and_original_images(self) -> None:
        tutorial = (REPO_ROOT / "docs" / "tutorials" / "fluxion-ai-getting-started.md").read_text(encoding="utf-8")
        self.assertIn("# 新手引导", tutorial)
        self.assertIn("## 一、快速上手 {#quick-start}", tutorial)
        self.assertIn("## 二、常见问题 {#faq}", tutorial)
        self.assertIn("## 三、获取帮助 {#support}", tutorial)
        self.assertEqual(len(re.findall(r"^\| ---", tutorial, flags=re.MULTILINE)), 3)
        image_references = re.findall(r"!\[[^\]]*\]\(\.\./images/fluxion-ai-getting-started/([^\)]+)\)", tutorial)
        self.assertEqual(image_references, [
            "01.png", "02.png", "03.png", "04.png", "05.png",
            "06.png", "07.png", "08.jpeg", "09.png",
        ])

        expected_hashes = {
            "01.png": "40a975fe1bc9595093ad136492c194fe5d236e9cc65e0ab9e6f42e106d8c1f01",
            "02.png": "16a2d31b8699141dbbf9c73ea1630b2b8f5714993bfacc434677b6c15b991b5f",
            "03.png": "338856c44224fcd0cb3e5fdcd218965aa5ba3bcbfc4215e33b9bd236033e1891",
            "04.png": "9a50558af2ddb02d25ac14a7b9732591e242afc00afee5336a0d669e5ff4113c",
            "05.png": "ffe8cd782074f985f1afd4a7399b0dc1ac8f5106f783d528e96d88dc9b2580a0",
            "06.png": "ef73e555f8c81909e5f4b362e4063fdbf23bbcc2c0b94fa2d7d019427a563bbd",
            "07.png": "e2d637a8b96299d8e64a6a5681676deb87f785fa84fa8439e646506cc157d2fc",
            "08.jpeg": "f73d9290ef35d33ce8866220da05b99a0eaaceae0c739a6362e18655db9691db",
            "09.png": "a4fffaef4c2de89603598ebfdd7a5427a940e78393181c3f975d546d38b461db",
        }
        image_root = REPO_ROOT / "docs" / "images" / "fluxion-ai-getting-started"
        actual_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(image_root.iterdir())
            if path.is_file()
        }
        self.assertEqual(actual_hashes, expected_hashes)

    def test_notice_document_loading_guards_against_stale_requests(self) -> None:
        app_js = (REPO_ROOT / "wandao_electron" / "renderer" / "app.js").read_text(encoding="utf-8")

        self.assertIn("selectedBodyId", app_js)
        self.assertIn("bodyCache", app_js)
        self.assertIn("bodyRequestSeq", app_js)
        self.assertIn("noticeCenterState.bodyRequestSeq !== requestSeq", app_js)
        self.assertIn("noticeCenterState.selectedId !== itemId", app_js)
        self.assertIn("bodyMatchesSelection", app_js)


if __name__ == "__main__":
    unittest.main()

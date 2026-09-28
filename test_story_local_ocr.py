import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ephemeral_ingest as ei


class StoryLocalOcrTests(unittest.TestCase):
    def test_extract_story_text_local_ocr_uses_bounded_tesseract(self):
        shot = Path("/tmp/story.png")
        with patch("ephemeral_ingest.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "  Oil   battleground\n123  "
            run.return_value.stderr = ""
            result = ei.extract_story_text_local_ocr(shot)

        self.assertEqual(result["text"], "Oil battleground\n123")
        self.assertEqual(result["source"], "LOCAL_OCR")
        self.assertEqual(result["provider"], "tesseract")
        args, kwargs = run.call_args
        self.assertIn("tesseract", args[0])
        self.assertEqual(kwargs["timeout"], ei.STORY_OCR_TIMEOUT_SECONDS)

    def test_ocr_quality_gate_rejects_symbol_heavy_garbage(self):
        noisy = (
            "Ins agzam sa x > ~ e f CT a 7 dä AA — 7 ; | +. 4 __WHENI "
            "=) STOP TRADING | £3, pROPTRADER ; [fins EDGE ‘ (— vv"
        )
        self.assertFalse(ei._story_ocr_text_sufficient(noisy))
        self.assertTrue(
            ei._story_ocr_text_sufficient(
                "Marknaden har pratat om säkerheten kring AI-agenter i flera veckor och i dag kom Nvidias svar."
            )
        )

    def test_ocr_quality_gate_rejects_real_fragmented_chart_capture(self):
        noisy = """Instagzam ik ll 2 i 0 VR ARKA x
lickarmilj @ ich
Vecka 40: räntan, Micron och jobben 82% ga
tainaeg nu) (Toupee SS) ANF
Bene |S. | ae oe Bee,
2 Seat | en [csr
— conti tong | eases tones | teen twig | 4) rec)
SE mey [BERT eee | frn
Imon a [RT | ite | armen
Tomei a | parece
= Annu en fantastisk live redo inför V.40 *
bd ey
——#i
°e a SS É mwah. J ©
rae al SIN er ais = Rage v
=I seas we =
7a ee 7 pee on
Fogo pao
—===
=
ov"""
        self.assertGreater(
            ei._story_ocr_fragmented_line_ratio(noisy),
            ei.STORY_OCR_MAX_FRAGMENTED_LINE_RATIO,
        )
        self.assertFalse(ei._story_ocr_text_sufficient(noisy))

    def test_ocr_quality_gate_keeps_usable_headline_capture(self):
        usable = """Instagzam x
tita omorrowpodcast and 1:37
refine ofi¢ems Of Tomorrow
/ @titans.of.tomorrow
Kathy Lien: The Stop-Hunting Story Is
More Complicated
; |
N DD
|
think tifeselbanks
4
ov"""
        self.assertLessEqual(
            ei._story_ocr_fragmented_line_ratio(usable),
            ei.STORY_OCR_MAX_FRAGMENTED_LINE_RATIO,
        )
        self.assertTrue(ei._story_ocr_text_sufficient(usable))

    def _manifest(self, root: Path, *, deferred=False):
        shot = root / "shot.png"
        shot.write_bytes(b"not-a-real-png")
        item = {
            "source_type": "STORY",
            "research_status": "NEW",
            "screenshot_file": "shot.png",
        }
        if deferred:
            item.update({
                "visual_description_status": "DEFERRED",
                "visual_description_deferred_reason": "PROVIDER_RATE_LIMIT",
                "visual_description_retry_after": "2099-01-01T00:00:00+00:00",
            })
        return {"items": {"story-1": item}}

    def test_sufficient_local_ocr_skips_ollama_and_gemini(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "one two three four five six seven eight",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_ollama") as ollama, patch(
                "ephemeral_ingest.extract_image_evidence_gemini"
            ) as gemini:
                result = ei.enrich_story_visual_evidence(root, manifest, ["story-1"])

            item = manifest["items"]["story-1"]
            self.assertEqual(item["visual_description_status"], "DONE")
            self.assertEqual(item["visual_description_source"], "LOCAL_OCR")
            self.assertEqual(item["visual_description_provider"], "tesseract")
            self.assertEqual(result["ocr_completed"], 1)
            self.assertEqual(result["ollama_attempted"], 0)
            self.assertEqual(result["attempted"], 0)
            ollama.assert_not_called()
            gemini.assert_not_called()

    def test_poor_ocr_is_recovered_by_ollama_before_gemini(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "Ins agzam x > ~ e f CT a 7 ; | STOP TRADING £3 ; EDGE",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_ollama", return_value={
                "text": "WHEN I STOP TRADING PROP TRADER EDGE with a visible chart and labels",
                "source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                "provider": "ollama",
                "model": "gemma3-12b-16k",
            }) as ollama, patch("ephemeral_ingest.extract_image_evidence_gemini") as gemini:
                result = ei.enrich_story_visual_evidence(root, manifest, ["story-1"])

            item = manifest["items"]["story-1"]
            self.assertEqual(item["visual_description_provider"], "ollama")
            self.assertEqual(result["ocr_insufficient"], 1)
            self.assertEqual(result["ollama_attempted"], 1)
            self.assertEqual(result["ollama_completed"], 1)
            self.assertEqual(result["attempted"], 0)
            ollama.assert_called_once()
            gemini.assert_not_called()

    def test_existing_low_quality_local_ocr_is_reprocessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            item = manifest["items"]["story-1"]
            item.update({
                "visual_description": "Ins agzam x > ~ e f CT a 7 ; | STOP TRADING £3 ; EDGE",
                "visual_description_status": "DONE",
                "visual_description_source": "LOCAL_OCR",
                "visual_description_provider": "tesseract",
            })
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "Ins agzam x > ~ e f CT a 7 ; | STOP TRADING £3 ; EDGE",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_ollama", return_value={
                "text": "WHEN I STOP TRADING PROP TRADER EDGE with a visible chart and labels",
                "source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                "provider": "ollama",
                "model": "gemma3-12b-16k",
            }), patch("ephemeral_ingest.extract_image_evidence_gemini") as gemini:
                result = ei.enrich_story_visual_evidence(root, manifest, ["story-1"])

            self.assertEqual(result["ollama_completed"], 1)
            self.assertEqual(item["visual_description_provider"], "ollama")
            gemini.assert_not_called()

    def test_existing_legacy_ollama_marker_is_reprocessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            item = manifest["items"]["story-1"]
            item.update({
                "visual_description": "Useful evidence\nNO_MEANINGFUL_VISUAL_EVIDENCE",
                "visual_description_status": "DONE",
                "visual_description_source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                "visual_description_provider": "ollama",
            })
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "tiny",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_ollama", return_value={
                "text": "Corrected concise visible Story evidence for downstream analysis",
                "source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                "provider": "ollama",
                "model": "gemma3-12b-16k",
            }), patch("ephemeral_ingest.extract_image_evidence_gemini") as gemini:
                result = ei.enrich_story_visual_evidence(root, manifest, ["story-1"])

            self.assertEqual(result["ollama_completed"], 1)
            self.assertNotIn("NO_MEANINGFUL_VISUAL_EVIDENCE", item["visual_description"])
            gemini.assert_not_called()

    def test_insufficient_ollama_falls_back_to_gemini(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "tiny",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_ollama", return_value={
                "text": "tiny",
                "source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                "provider": "ollama",
                "model": "gemma3-12b-16k",
            }), patch("ephemeral_ingest.extract_image_evidence_gemini", return_value={
                "text": "rich visual evidence with enough information for downstream analysis",
                "source": "GEMINI_STORY_SCREENSHOT_EVIDENCE",
                "provider": "gemini",
                "model": "test-model",
            }) as gemini:
                result = ei.enrich_story_visual_evidence(root, manifest, ["story-1"])

            self.assertEqual(result["ocr_insufficient"], 1)
            self.assertEqual(result["ollama_insufficient"], 1)
            self.assertEqual(result["attempted"], 1)
            self.assertEqual(manifest["items"]["story-1"]["visual_description_provider"], "gemini")
            gemini.assert_called_once()

    def test_ollama_can_recover_during_gemini_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root, deferred=True)
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "tiny",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_ollama", return_value={
                "text": "local multimodal evidence is sufficiently detailed for analysis now",
                "source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                "provider": "ollama",
                "model": "gemma3-12b-16k",
            }), patch("ephemeral_ingest.extract_image_evidence_gemini") as gemini:
                result = ei.enrich_story_visual_evidence(
                    root,
                    manifest,
                    ["story-1"],
                    circuit_state={
                        "open": True,
                        "reason": "PROVIDER_RATE_LIMIT",
                        "retry_after": "2099-01-01T00:00:00+00:00",
                        "retry_after_source": "FALLBACK",
                    },
                )

            item = manifest["items"]["story-1"]
            self.assertEqual(item["visual_description_status"], "DONE")
            self.assertEqual(item["visual_description_provider"], "ollama")
            self.assertNotIn("visual_description_retry_after", item)
            self.assertEqual(result["ollama_completed"], 1)
            gemini.assert_not_called()

    def test_existing_ollama_without_current_contract_is_reprocessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            item = manifest["items"]["story-1"]
            item.update({
                "visual_description": "Old local model evidence that looks plausible but predates grounding",
                "visual_description_status": "DONE",
                "visual_description_source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                "visual_description_provider": "ollama",
            })
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "tiny",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_ollama", return_value={
                "text": "Corrected visible Story text with enough grounded words for analysis",
                "source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                "provider": "ollama",
                "model": "gemma3-12b-16k",
                "contract": ei.OLLAMA_VISUAL_CONTRACT,
            }), patch("ephemeral_ingest.extract_image_evidence_gemini") as gemini:
                result = ei.enrich_story_visual_evidence(root, manifest, ["story-1"])

            self.assertEqual(result["ollama_completed"], 1)
            self.assertEqual(item["visual_description_contract"], ei.OLLAMA_VISUAL_CONTRACT)
            gemini.assert_not_called()

    def test_shared_ollama_budget_is_global_across_enrichment_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "control").mkdir(parents=True)
            (root / "control" / "settings.json").write_text(
                '{"transcription":{"ollama_visual_max_attempts":1}}',
                encoding="utf-8",
            )
            shared_budget = {"attempted": 0}

            def manifest_for(name: str):
                shot = root / f"{name}.png"
                shot.write_bytes(b"not-a-real-png")
                return {
                    "items": {
                        name: {
                            "source_type": "STORY",
                            "research_status": "NEW",
                            "screenshot_file": f"{name}.png",
                        }
                    }
                }

            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "tiny",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_ollama", return_value={
                "text": "Corrected visible Story evidence with enough grounded words for analysis",
                "source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                "provider": "ollama",
                "model": "gemma3-12b-16k",
                "contract": ei.OLLAMA_VISUAL_CONTRACT,
            }) as ollama, patch("ephemeral_ingest.extract_image_evidence_gemini", return_value={
                "text": "Gemini fallback evidence with enough visible text for analysis",
                "source": "GEMINI_STORY_SCREENSHOT_EVIDENCE",
                "provider": "gemini",
                "model": "test-model",
            }) as gemini:
                first = manifest_for("story-1")
                result_one = ei.enrich_story_visual_evidence(
                    root,
                    first,
                    ["story-1"],
                    ollama_budget_state=shared_budget,
                )
                second = manifest_for("story-2")
                result_two = ei.enrich_story_visual_evidence(
                    root,
                    second,
                    ["story-2"],
                    ollama_budget_state=shared_budget,
                )

            self.assertEqual(ollama.call_count, 1)
            self.assertEqual(gemini.call_count, 1)
            self.assertEqual(shared_budget["attempted"], 1)
            self.assertEqual(shared_budget["limit"], 1)
            self.assertEqual(result_one["ollama_attempted"], 1)
            self.assertEqual(result_two["ollama_attempted"], 0)
            self.assertEqual(second["items"]["story-2"]["visual_description_provider"], "gemini")

    def test_gemini_rate_limit_is_deferred_not_fatal(self):
        class RateLimitError(Exception):
            code = 429
            status = "RESOURCE_EXHAUSTED"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._manifest(root)
            with patch("ephemeral_ingest.extract_story_text_local_ocr", return_value={
                "text": "tiny",
                "source": "LOCAL_OCR",
                "provider": "tesseract",
                "model": "eng+swe",
            }), patch("ephemeral_ingest.extract_image_evidence_ollama", return_value={
                "text": "",
                "source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
                "provider": "ollama",
                "model": "gemma3-12b-16k",
                "contract": ei.OLLAMA_VISUAL_CONTRACT,
            }), patch(
                "ephemeral_ingest.extract_image_evidence_gemini",
                side_effect=RateLimitError(),
            ):
                result = ei.enrich_story_visual_evidence(root, manifest, ["story-1"])

            item = manifest["items"]["story-1"]
            self.assertEqual(item["visual_description_status"], "DEFERRED")
            self.assertEqual(item["visual_description_deferred_reason"], "PROVIDER_RATE_LIMIT")
            self.assertEqual(result["errors"], [])
            self.assertEqual(len(result["provider_events"]), 1)
            self.assertIn("ollama_total_ms", result["timings"])
            self.assertIn("gemini_total_ms", result["timings"])

    def test_legacy_ollama_without_contract_requires_enrichment(self):
        item = {
            "visual_description": "Visible Story text that was produced by the old local model path",
            "visual_description_status": "DONE",
            "visual_description_source": "OLLAMA_STORY_SCREENSHOT_EVIDENCE",
            "visual_description_provider": "ollama",
        }
        self.assertTrue(ei._story_visual_needs_enrichment(item))
        item["visual_description_contract"] = ei.OLLAMA_VISUAL_CONTRACT
        self.assertFalse(ei._story_visual_needs_enrichment(item))


if __name__ == "__main__":
    unittest.main()

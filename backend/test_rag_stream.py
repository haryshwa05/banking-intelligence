import os
import unittest
from unittest.mock import MagicMock, patch

from rag import RagEngine


class RagStreamingTests(unittest.TestCase):
    def test_split_tags_stream_answer_and_validate_citations(self) -> None:
        engine = object.__new__(RagEngine)
        stream = MagicMock()
        stream.__enter__.return_value.text_stream = iter([
            '<ans', 'wer>Tony requested ', '₹8,00,000.</ans', 'wer><citations>',
            '["c1", "fabricated"]', '</citations>',
        ])
        client = MagicMock()
        client.return_value.messages.stream.return_value = stream
        chunks = [{"id": "c1", "original_name": "loan.pdf", "page_number": 2, "text": "Requested amount ₹8,00,000"}]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), patch("anthropic.Anthropic", client):
            events = list(engine.answer_stream("How much?", chunks))
        self.assertEqual("".join(item["text"] for item in events if item["type"] == "delta"), "Tony requested ₹8,00,000.")
        self.assertEqual(events[-1]["citedChunkIds"], ["c1"])

    def test_unvalidated_output_is_replaced_with_safe_answer(self) -> None:
        engine = object.__new__(RagEngine)
        stream = MagicMock()
        stream.__enter__.return_value.text_stream = iter(["<answer>Unsupported claim</answer><citations>[]</citations>"])
        client = MagicMock()
        client.return_value.messages.stream.return_value = stream
        chunks = [{"id": "c1", "original_name": "loan.pdf", "page_number": 2, "text": "No relevant value"}]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), patch("anthropic.Anthropic", client), patch.object(engine, "answer", return_value=("Unverified", [])):
            events = list(engine.answer_stream("How much?", chunks))
        self.assertEqual(events[-1]["answer"], "I could not verify an answer against the selected evidence.")
        self.assertEqual(events[-1]["citedChunkIds"], [])


if __name__ == "__main__":
    unittest.main()

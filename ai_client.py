import base64
import difflib
import hashlib
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from google import genai

try:
    import docx
except ImportError:
    docx = None

FILE_CACHE_METADATA = ".gemini_file_cache.json"
CACHE_TTL_SECONDS = 45 * 3600  # Gemini Files API files expire after 48h; refresh after 45h

VALID_LETTERS = ("a", "b", "c", "d", "e", "f")

QUIZ_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "question_found": {
            "type": "boolean",
            "description": (
                "True jeśli na zrzucie ekranu znajduje się pytanie zamknięte/testowe "
                "z wariantami odpowiedzi; False w przeciwnym razie."
            ),
        },
        "screen_options": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Lista WSZYSTKICH wariantów odpowiedzi widocznych NA ZRZUCIE EKRANU "
                "w dokładnej kolejności wyświetlania (1.=a, 2.=b, 3.=c, 4.=d...), "
                "bez wiodących punktorów ani liter."
            ),
        },
        "correct_answer_text": {
            "type": "string",
            "description": (
                "Dokładna treść poprawnej odpowiedzi ustalona z plików użytkownika lub Google Search, "
                "bez kopiowania litery (a/b/c/d) z plików źródłowych."
            ),
        },
        "screen_position": {
            "type": "integer",
            "description": (
                "Pozycja (liczona od 1) poprawnej odpowiedzi w liście screen_options "
                "na zrzucie ekranu, albo 0 jeśli question_found=false."
            ),
        },
        "letter": {
            "type": "string",
            "description": "Mała litera przypisana do pozycji na ekranie: a, b, c, d, e, f lub ?.",
        },
    },
    "required": [
        "question_found",
        "screen_options",
        "correct_answer_text",
        "screen_position",
        "letter",
    ],
}

SYSTEM_PROMPT = (
    "Jesteś błyskawicznym, niezwykle dokładnym systemem rozwiązującym pytania testowe wielokrotnego wyboru.\n"
    "Twoje zadanie krok po kroku:\n"
    "1. ZNAJDŹ PYTANIE NA EKRANIE: Odszukaj na zrzucie ekranu pytanie zamknięte wraz z listą wariantów odpowiedzi — "
    "RÓWNIEŻ wtedy, gdy warianty na ekranie NIE mają żadnych liter (A, B, C, D), a jedynie punktory (`-`, `•`), "
    "kółka wyboru (radio buttons), pola wyboru (checkboxy) lub kolejne wiersze tekstu.\n"
    "2. SPISZ WARIANTY Z EKRANU (`screen_options`): Przepisz dokładnie wszystkie warianty odpowiedzi widoczne NA ZRZUCIE EKRANU "
    "w kolejności od góry do dołu (a przy układzie siatki 2x2: od lewej do prawej w górnym rzędzie, potem od lewej do prawej w dolnym; "
    "jeśli na ekranie są jawne litery A, B, C, D — zachowaj ich kolejność). Usuń wiodące punktory (`-`, `•`, `*`) i ewentualne etykiety (`a.`, `1.`). "
    "Kolejność na ekranie wyznacza litery: 1. opcja = `a`, 2. opcja = `b`, 3. opcja = `c`, 4. opcja = `d` (opcjonalnie 5. = `e`, 6. = `f`).\n"
    "3. ZASADA KRYTYCZNA (DOPASOWANIE PO TREŚCI, NIE PO LITERZE Z PLIKU): NIGDY nie przepisuj samej litery (`a`, `b`, `c`, `d`) z plików użytkownika! "
    "Na egzaminie kolejność odpowiedzi bywa przemieszana względem bazy pytań (np. w pliku z notatkami poprawna odpowiedź `* a. wykonanie artefaktów` "
    "może być na ekranie jako 4. opcja `- wykonanie artefaktów`, więc poprawna odpowiedź na ekranie to `d`, a NIE `a`!).\n"
    "4. USTAL TREŚĆ POPRAWNEJ ODPOWIEDZI (`correct_answer_text`):\n"
    "   - PRIORYTET 1 (Pliki użytkownika): Najpierw dokładnie przeszukaj załączone dokumenty użytkownika. Rozpoznawaj oznaczenia poprawnych odpowiedzi "
    "(np. `*`, `[x]`, `**pogrubienie**`, `[*podświetlenie*]`, `Poprawna:`, klucz odpowiedzi lub treść notatek) i wyciągnij TREŚĆ poprawnej odpowiedzi.\n"
    "   - PRIORYTET 2 (Google Search): Jeśli odpowiedzi nie ma w plikach użytkownika lub pliki nie zostały załączone, zweryfikuj fakty w Google Search.\n"
    "5. WSKAŻ POZYCJĘ NA EKRANIE: Dopasuj `correct_answer_text` do listy `screen_options`, podaj numer pozycji `screen_position` (od 1) "
    "oraz odpowiednią małą literę `letter` (`a`–`f`). Jeśli na ekranie nie ma pytania testowego, ustaw `question_found: false`, `screen_options: []`, "
    "`correct_answer_text: \"\"`, `screen_position: 0`, `letter: \"?\"`.\n"
    "Zwróć wyłącznie poprawny obiekt JSON zgodny ze schematem."
)


class KnowledgeContextManager:
    """
    Manages user source files (PDF, TXT, DOCX, MD):
    - PDF files are uploaded once via the Gemini Files API (`client.files.upload`) and cached by URI (valid for 48h)
      so they are not re-uploaded on every query.
    - TXT, MD, and DOCX files are loaded into a unified text block in RAM (leveraging
      automatic Implicit Context Caching in the Gemini Interactions API).
    """

    def __init__(self):
        self._cached_folder: str = ""
        self._cached_fingerprint: str = ""
        self._text_context_block: str = ""
        self._uploaded_pdf_blocks: List[Dict[str, Any]] = []

    def _compute_folder_fingerprint(self, folder_path: Path) -> Tuple[str, List[Path]]:
        supported_exts = {".pdf", ".txt", ".md", ".docx"}
        files: List[Path] = []
        if not folder_path.exists() or not folder_path.is_dir():
            return "", []

        for root, _, filenames in os.walk(folder_path):
            for fname in sorted(filenames):
                if fname == FILE_CACHE_METADATA or fname.startswith("~$"):
                    continue
                fpath = Path(root) / fname
                if fpath.suffix.lower() in supported_exts:
                    files.append(fpath)

        hasher = hashlib.sha256()
        for f in files:
            try:
                stat = f.stat()
                hasher.update(f"{f.resolve()}:{stat.st_mtime_ns}:{stat.st_size}|".encode("utf-8"))
            except OSError:
                continue
        return hasher.hexdigest(), files

    @staticmethod
    def _format_docx_paragraph(paragraph) -> str:
        runs = getattr(paragraph, "runs", None) or []
        raw_text = paragraph.text.strip()
        if not raw_text or not runs:
            return raw_text

        non_empty_runs = [r for r in runs if r.text and r.text.strip()]
        if not non_empty_runs:
            return raw_text

        has_highlight = any(getattr(r.font, "highlight_color", None) is not None for r in non_empty_runs)
        has_bold = any(bool(r.bold) for r in non_empty_runs)
        has_non_bold = any(not bool(r.bold) for r in non_empty_runs)
        whole_bold = has_bold and not has_non_bold and len(raw_text) <= 180

        if whole_bold and not has_highlight:
            return f"**{raw_text}**"

        parts: List[str] = []
        for r in runs:
            txt = r.text or ""
            if not txt:
                continue
            stripped = txt.strip()
            if not stripped:
                parts.append(txt)
                continue
            if getattr(r.font, "highlight_color", None) is not None:
                parts.append(txt.replace(stripped, f"[*{stripped}*]"))
            elif r.bold and (has_non_bold or whole_bold):
                parts.append(txt.replace(stripped, f"**{stripped}**"))
            else:
                parts.append(txt)

        return "".join(parts).strip()

    def _extract_docx_text(self, path: Path) -> str:
        if docx is None:
            return ""
        try:
            document = docx.Document(str(path))
            paragraphs: List[str] = []
            for p in document.paragraphs:
                formatted = self._format_docx_paragraph(p)
                if formatted:
                    paragraphs.append(formatted)
            # DOCX tables also frequently contain questions and answers
            for table in document.tables:
                for row in table.rows:
                    cell_texts: List[str] = []
                    for cell in row.cells:
                        cell_parts = [
                            self._format_docx_paragraph(cp)
                            for cp in cell.paragraphs
                        ]
                        joined_cell = " ".join(part for part in cell_parts if part).strip()
                        if joined_cell:
                            cell_texts.append(joined_cell)
                    row_text = " | ".join(cell_texts)
                    if row_text:
                        paragraphs.append(row_text)
            return "\n".join(paragraphs)
        except Exception as e:
            print(f"[KnowledgeContextManager] Błąd odczytu DOCX ({path.name}): {e}")
            return ""

    def _load_or_upload_pdfs(
        self,
        client: genai.Client,
        folder_path: Path,
        pdf_files: List[Path],
        status_callback: Optional[Callable[[str], None]] = None
    ) -> List[Dict[str, Any]]:
        if not pdf_files:
            return []

        cache_file = folder_path / FILE_CACHE_METADATA
        cache_data: Dict[str, Any] = {}
        now = time.time()

        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
            except Exception:
                cache_data = {}

        pdf_blocks: List[Dict[str, Any]] = []
        cache_modified = False

        for pdf_path in pdf_files:
            rel_key = str(pdf_path.relative_to(folder_path))
            stat = pdf_path.stat()
            file_sig = f"{stat.st_mtime_ns}:{stat.st_size}"

            entry = cache_data.get(rel_key)
            if (
                entry
                and entry.get("sig") == file_sig
                and (now - entry.get("uploaded_at", 0)) < CACHE_TTL_SECONDS
                and entry.get("uri")
            ):
                pdf_blocks.append({
                    "type": "document",
                    "uri": entry["uri"],
                    "mime_type": entry.get("mime_type", "application/pdf"),
                })
                continue

            # Upload to the Gemini Files API
            if status_callback:
                status_callback(f"Przesyłanie pliku PDF do cache Gemini: {pdf_path.name}...")
            try:
                uploaded = client.files.upload(file=str(pdf_path))
                uri = getattr(uploaded, "uri", None)
                mime_type = getattr(uploaded, "mime_type", "application/pdf") or "application/pdf"
                if uri:
                    pdf_blocks.append({
                        "type": "document",
                        "uri": uri,
                        "mime_type": mime_type,
                    })
                    cache_data[rel_key] = {
                        "sig": file_sig,
                        "uri": uri,
                        "mime_type": mime_type,
                        "uploaded_at": now,
                    }
                    cache_modified = True
            except Exception as e:
                print(f"[KnowledgeContextManager] Błąd uploadu PDF ({pdf_path.name}): {e}")

        if cache_modified:
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(cache_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[KnowledgeContextManager] Nie udało się zapisać cache pliku: {e}")

        return pdf_blocks

    def prepare_context_blocks(
        self,
        client: genai.Client,
        source_folder: str,
        status_callback: Optional[Callable[[str], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Return a list of input blocks (Files API PDF documents + aggregated TXT/MD/DOCX text)
        ready to be passed into `input` of `client.interactions.create`.
        """
        if not source_folder:
            return []

        folder_path = Path(source_folder)
        fingerprint, files = self._compute_folder_fingerprint(folder_path)
        if not files:
            return []

        if folder_path.as_posix() != self._cached_folder or fingerprint != self._cached_fingerprint:
            text_parts: List[str] = []
            pdf_files: List[Path] = []

            for fpath in files:
                ext = fpath.suffix.lower()
                if ext == ".pdf":
                    pdf_files.append(fpath)
                elif ext in {".txt", ".md"}:
                    try:
                        content = fpath.read_text(encoding="utf-8", errors="ignore").strip()
                        if content:
                            text_parts.append(f"=== PLIK ŹRÓDŁOWY: {fpath.name} ===\n{content}")
                    except Exception as e:
                        print(f"[KnowledgeContextManager] Błąd odczytu {fpath.name}: {e}")
                elif ext == ".docx":
                    content = self._extract_docx_text(fpath)
                    if content:
                        text_parts.append(f"=== PLIK ŹRÓDŁOWY: {fpath.name} ===\n{content}")

            self._uploaded_pdf_blocks = self._load_or_upload_pdfs(
                client, folder_path, pdf_files, status_callback
            )
            if text_parts:
                self._text_context_block = (
                    "BAZA WIEDZY UŻYTKOWNIKA (Priorytet 1 - sprawdź przed wyszukiwaniem w sieci):\n\n"
                    + "\n\n".join(text_parts)
                )
            else:
                self._text_context_block = ""

            self._cached_folder = folder_path.as_posix()
            self._cached_fingerprint = fingerprint

        blocks: List[Dict[str, Any]] = []
        blocks.extend(self._uploaded_pdf_blocks)
        if self._text_context_block:
            blocks.append({"type": "text", "text": self._text_context_block})
        return blocks

    def invalidate(self) -> None:
        self._cached_folder = ""
        self._cached_fingerprint = ""
        self._text_context_block = ""
        self._uploaded_pdf_blocks = []


class GeminiQuizAnalyzer:
    """
    Client that analyzes multiple-choice test screenshots using the official `google-genai` SDK
    and the modern `client.interactions.create` API.
    """

    def __init__(self):
        self.context_manager = KnowledgeContextManager()

    @staticmethod
    def _normalize_option_text(text: str, strip_accents: bool = False) -> str:
        s = text.strip().lower()
        s = re.sub(r"^\[\*(.*)\*\]$", r"\1", s).strip()
        s = re.sub(r"^\*\*(.*)\*\*$", r"\1", s).strip()

        prev = None
        while s != prev:
            prev = s
            s = re.sub(r"^(?:[*•\-–—○●☐☑✓✔]+\s*|\[\s*[xX]?\s*\]\s*)+", "", s)
            s = re.sub(r"^(?:[a-f]|[1-6])[.)\]:]\s+", "", s)

        s = re.sub(r"\s+", " ", s).rstrip(".,;:!? ")
        if strip_accents:
            s = s.replace("ł", "l")
            s = "".join(
                ch for ch in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(ch)
            )
        return s

    @classmethod
    def _match_answer_to_screen_options(
        cls,
        correct_text: str,
        screen_options: List[str]
    ) -> Optional[int]:
        if not correct_text or not screen_options:
            return None

        # Pass 1: exact normalized match (with diacritics preserved)
        norm_c = cls._normalize_option_text(correct_text, strip_accents=False)
        norm_opts = [cls._normalize_option_text(opt, strip_accents=False) for opt in screen_options]
        if norm_c:
            exact_hits = [i for i, opt in enumerate(norm_opts) if opt and opt == norm_c]
            if len(exact_hits) == 1:
                return exact_hits[0]

        # Pass 2: exact normalized match without diacritics (ASCII folding)
        norm_c_ascii = cls._normalize_option_text(correct_text, strip_accents=True)
        norm_opts_ascii = [cls._normalize_option_text(opt, strip_accents=True) for opt in screen_options]
        if not norm_c_ascii:
            return None

        ascii_hits = [i for i, opt in enumerate(norm_opts_ascii) if opt and opt == norm_c_ascii]
        if len(ascii_hits) == 1:
            return ascii_hits[0]

        # Pass 3: unique substring containment
        if len(norm_c_ascii) >= 4:
            sub_hits: List[int] = []
            for i, opt in enumerate(norm_opts_ascii):
                if not opt:
                    continue
                if norm_c_ascii in opt or opt in norm_c_ascii:
                    ratio = min(len(norm_c_ascii), len(opt)) / float(max(len(norm_c_ascii), len(opt)))
                    if ratio >= 0.45:
                        sub_hits.append(i)
            if len(sub_hits) == 1:
                return sub_hits[0]

        # Pass 4: fuzzy match (difflib.SequenceMatcher) with confidence margin
        scores: List[Tuple[float, int]] = []
        for i, opt in enumerate(norm_opts_ascii):
            if not opt:
                continue
            ratio = difflib.SequenceMatcher(None, norm_c_ascii, opt).ratio()
            scores.append((ratio, i))
        if scores:
            scores.sort(key=lambda item: item[0], reverse=True)
            best_ratio, best_idx = scores[0]
            if best_ratio >= 0.78 and (len(scores) == 1 or (best_ratio - scores[1][0]) >= 0.08):
                return best_idx

        return None

    @staticmethod
    def parse_answer_letter(raw_output: Optional[str]) -> str:
        """
        Extract a single lowercase answer letter ('a'–'f') or '?' from the model's text output.
        """
        if not raw_output:
            return "?"
        cleaned = raw_output.strip().lower()
        if cleaned in set(VALID_LETTERS) | {"?"}:
            return cleaned

        # Look for patterns like "answer: b" or a standalone letter a-f
        match = re.search(r"\b([a-f])\b", cleaned)
        if match:
            return match.group(1)
        if "?" in cleaned:
            return "?"
        return "?"

    @staticmethod
    def _extract_json_dict(raw_output: str) -> Optional[Dict[str, Any]]:
        text = raw_output.strip()
        if not text:
            return None

        # Strip optional markdown ```json ... ``` code fence
        fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        for candidate in (text, fenced):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

        # Fallback: first valid JSON object inside the text
        idx = text.find("{")
        if idx != -1:
            try:
                parsed, _ = json.JSONDecoder().raw_decode(text[idx:])
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return None

    @classmethod
    def resolve_structured_answer(cls, raw_output: Optional[str]) -> str:
        """
        Deterministically resolve the answer letter ('a'–'f' or '?') from the
        on-screen option order (`screen_options`) and the correct answer text
        (`correct_answer_text`), overriding any mistaken source-file letter.
        """
        if not raw_output:
            return "?"

        data = cls._extract_json_dict(raw_output)
        if isinstance(data, dict):
            if data.get("question_found") is False:
                print("[AnswearPrint AI] Brak pytania testowego na ekranie -> '?'")
                return "?"

            screen_options = [
                str(x).strip()
                for x in (data.get("screen_options") or [])
                if str(x).strip()
            ]
            correct_text = str(data.get("correct_answer_text") or "").strip()

            matched_idx = cls._match_answer_to_screen_options(correct_text, screen_options)
            if matched_idx is not None and 0 <= matched_idx < len(VALID_LETTERS):
                letter = VALID_LETTERS[matched_idx]
                print(
                    f"[AnswearPrint AI] Dopasowano po treści: '{correct_text[:60]}' "
                    f"-> poz. {matched_idx + 1} ('{letter}') [opcji: {len(screen_options)}]"
                )
                return letter

            screen_position = data.get("screen_position")
            if (
                isinstance(screen_position, int)
                and not isinstance(screen_position, bool)
                and 1 <= screen_position <= len(VALID_LETTERS)
                and (not screen_options or screen_position <= len(screen_options))
            ):
                letter = VALID_LETTERS[screen_position - 1]
                print(f"[AnswearPrint AI] Wybrano wg pozycji na ekranie: poz. {screen_position} ('{letter}')")
                return letter

            return cls.parse_answer_letter(str(data.get("letter") or ""))

        return cls.parse_answer_letter(raw_output)

    def analyze_screenshot(
        self,
        jpeg_bytes: bytes,
        api_key: str,
        model: str = "gemini-4-hydrogen",
        source_folder: str = "",
        thinking_level: str = "low",
        use_google_search: bool = True,
        timeout_seconds: float = 30.0,
        status_callback: Optional[Callable[[str], None]] = None
    ) -> str:
        """
        Send a screenshot along with user source file context and Google Search Grounding enabled.
        Return a single character: 'a'–'f' or '?'.
        """
        resolved_key = api_key.strip() or os.environ.get("GEMINI_API_KEY", "").strip()
        if not resolved_key:
            raise ValueError("Brak klucza Gemini API. Ustaw klucz w Ustawieniach lub zmiennej GEMINI_API_KEY.")

        # Timeout in milliseconds for http_options and in seconds for interactions.create
        clamped_timeout = min(max(timeout_seconds, 5.0), 30.0)
        timeout_ms = int(clamped_timeout * 1000)
        client = genai.Client(
            api_key=resolved_key,
            http_options={"timeout": timeout_ms}
        )

        # Automatically map deprecated model names to the fast multimodal Gemini 4 Hydrogen model
        target_model = model.strip() or "gemini-4-hydrogen"
        if target_model.startswith(("gemini-2.5", "gemini-2.0", "gemini-1.5")):
            target_model = "gemini-4-hydrogen"

        input_blocks: List[Dict[str, Any]] = []

        # Priority 1: Context from user files (PDF via Files API + text from TXT/MD/DOCX)
        context_blocks = self.context_manager.prepare_context_blocks(
            client=client,
            source_folder=source_folder,
            status_callback=status_callback
        )
        input_blocks.extend(context_blocks)

        # In-memory screenshot image (inline base64)
        b64_image = base64.b64encode(jpeg_bytes).decode("utf-8")
        input_blocks.append({
            "type": "image",
            "data": b64_image,
            "mime_type": "image/jpeg"
        })

        input_blocks.append({
            "type": "text",
            "text": (
                "Przeanalizuj pytanie testowe widoczne na zrzucie ekranu (również gdy opcje nie mają liter a/b/c/d, "
                "a jedynie punktory lub pola wyboru). Wypisz w `screen_options` wszystkie warianty w dokładnej kolejności "
                "widocznej NA EKRANIE, ustal treść poprawnej odpowiedzi `correct_answer_text` (Priorytet 1: pliki użytkownika, "
                "Priorytet 2: Google Search — ignorując oryginalne litery w plikach!) i wskaż pozycję `screen_position` oraz `letter`."
            )
        })

        tools: List[Dict[str, Any]] = []
        if use_google_search:
            tools.append({"type": "google_search"})

        gen_config: Dict[str, Any] = {}
        # gemini-4-* and gemini-3.5-* models support thinking_level (e.g. "low" for <5s latency)
        if thinking_level in {"minimal", "low", "medium", "high"}:
            if target_model.startswith("gemini-4-") and thinking_level == "minimal":
                gen_config["thinking_level"] = "low"
            else:
                gen_config["thinking_level"] = thinking_level

        create_kwargs: Dict[str, Any] = {
            "model": target_model,
            "system_instruction": SYSTEM_PROMPT,
            "input": input_blocks,
            "response_format": [
                {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": QUIZ_RESPONSE_SCHEMA,
                }
            ],
        }
        if tools:
            create_kwargs["tools"] = tools
        if gen_config:
            create_kwargs["generation_config"] = gen_config

        try:
            interaction = client.interactions.create(**create_kwargs, timeout=clamped_timeout)
        except Exception as exc:
            err_lower = str(exc).lower()
            if "response_format" in err_lower or "schema" in err_lower:
                print(f"[AnswearPrint AI] Model nie przyjął response_format, ponawiam bez schematu: {exc}")
                create_kwargs.pop("response_format", None)
                interaction = client.interactions.create(**create_kwargs, timeout=clamped_timeout)
            else:
                raise

        raw_text = getattr(interaction, "output_text", None)

        # Fallback if output_text is empty and the answer is inside steps
        if not raw_text and getattr(interaction, "steps", None):
            for step in reversed(interaction.steps):
                if getattr(step, "type", "") == "model_output" and getattr(step, "content", None):
                    for part in step.content:
                        if getattr(part, "type", "") == "text" and getattr(part, "text", ""):
                            raw_text = part.text
                            break
                if raw_text:
                    break

        return self.resolve_structured_answer(raw_text)

import asyncio
import cv2
import numpy as np
from io import BytesIO
from pathlib import Path
from PIL import Image
from docx import Document
from pptx import Presentation
from google import genai
import fitz
import os
import asyncio
import time
from app.logging_config import get_logger
from services.llm_gateway import estimate_tokens
from services.llm_provider import RATE_LIMIT_ERRORS

MAX_INPUT_TOKENS_PER_CALL = 4200
MAX_CHUNKS_PER_BATCH = 4

logger = get_logger(__name__)

def make_extraction_batches(chunks):
    batches, current, current_tokens = [], [], 0
    for chunk in chunks:
        chunk_tokens = estimate_tokens(chunk["text"])
        if current and (len(current) >= MAX_CHUNKS_PER_BATCH or current_tokens + chunk_tokens > MAX_INPUT_TOKENS_PER_CALL):
            batches.append(current)
            current, current_tokens = [], 0
        current.append(chunk)
        current_tokens += chunk_tokens
    if current:
        batches.append(current)
    return batches


async def extract_with_exp_backoff(engine, chunks, known_concepts_by_chunk=None, batch_index=None, total_batches=None, retries=5):
    delay = 5
    started = time.perf_counter()
    for attempt in range(1, retries + 1):
        try:
            data = await engine.extract_knowledge_batch(chunks, known_concepts_by_chunk=known_concepts_by_chunk)
            logger.info("extraction_batch_complete batch=%s/%s chunks=%d chars=%d concepts=%d relationships=%d attempts=%d duration_ms=%.1f", batch_index, total_batches, len(chunks), sum(len(chunk["text"]) for chunk in chunks), len(data.get("concepts", [])), len(data.get("relationships", [])), attempt, (time.perf_counter() - started) * 1000)
            return data
        except RATE_LIMIT_ERRORS:
            logger.warning("extraction_batch_rate_limited batch=%s/%s attempt=%d/%d", batch_index, total_batches, attempt, retries)
            if attempt == retries:
                break
            await asyncio.sleep(delay)
            delay *= 2
    raise Exception("Maximum retries exceeded!")

# Initialize EasyOCR reader (better for handwriting than Paddle)
reader = None
_ai_client = None

def get_ocr_reader():
    global reader
    if reader is None:
        import easyocr

        reader = easyocr.Reader(["en"], gpu=False)
    return reader

def get_ai_client():
    global _ai_client
    if _ai_client is None:
        key = os.getenv("GEMINI_VISION_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_VISION_API_KEY is not configured!."
            )
        _ai_client = genai.Client(api_key=key)
    return _ai_client


async def extract_text(file_bytes, filename=None):
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        return await extract_pdf(file_bytes)

    elif suffix == ".docx":
        return extract_word_doc(file_bytes)

    elif suffix == ".pptx":
        return extract_pptx(file_bytes)

    elif suffix in [".txt", ".md"]:
        return [{"page": 1, "text": file_bytes.decode("utf-8-sig")}]

    elif suffix in [".png", ".jpg", ".jpeg"]:
        return await extract_image_ocr(file_bytes)

    else:
        raise ValueError("File is not supported!")


async def extract_pdf(file_bytes):
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages = []

    for i, page in enumerate(doc, start=1):
        text = page.get_text().strip()

        # if the page contains minimal digital text, its likely a scan or image page
        if len(text) < 10:
            # render pdf page to a high res image
            pix = page.get_pixmap(dpi=150)
            img_bytes = pix.tobytes("png")
            # extract the text from the image
            ocr_res = await extract_image_ocr(img_bytes)
            ocr_text = ocr_res[0]["text"]

            pages.append(
                {
                    "page": i,
                    "text": ocr_text,
                    "source": ocr_res[0].get("source", "ocr"),
                    "is_virtual_page": False,
                }
            )
        else:
            pages.append(
                {
                    "page": i,
                    "text": text,
                    "source": "native_pdf",
                    "is_virtual_page": False,
                }
            )
    return pages


def extract_word_doc(file_bytes):
    """
    DOCX files do not have physical pages

    Includes paragraphs + tables and flags virtual pages for citations.
    """
    document = Document(BytesIO(file_bytes))
    pages = []
    para_text = []
    # extract paragraphs
    for para in document.paragraphs:
        if para.text.strip():
            para_text.append(para.text.strip())

    # extract tables from the word document
    for table in document.tables:
        for row in table.rows:
            row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            # format it as a table in the returned text
            if row_text:
                para_text.append("|".join(row_text))

    full_text = "\n".join(para_text)

    # word documents dont support pages when parsed
    # so we set a virtual page boundary of 3000
    pages = []
    chunk_size = 3000
    for i in range(0, len(full_text), chunk_size):
        chunk = full_text[i : i + chunk_size]
        pages.append(
            {
                "page": (i // chunk_size) + 1,
                "text": chunk.strip(),
                "is_virtual_page": True,
            }
        )

    return pages if pages else [{"page": 1, "text": "", "is_virtual_page": True}]

def _extract_shape_content(shape):
    """Helper to handle shapes, tables, and nested group shapes."""
    items = []

    if shape.is_placeholder:
        ph_type = str(shape.placeholder_format.type)
        if any(
            noise in ph_type for noise in ["FOOTER", "SLIDE_NUMBER", "HEADER"]
        ):
            return items

    if shape.has_text_frame:
        text = shape.text_frame.text.strip()
        if text:
            items.append((shape.top, shape.left, text))

    elif shape.has_table:
        table_lines = []
        for row in shape.table.rows:
            row_text = [
                cell.text.strip() for cell in row.cells if cell.text.strip()
            ]
            if row_text:
                table_lines.append(" | ".join(row_text))
        if table_lines:
            items.append((shape.top, shape.left, "\n".join(table_lines)))
    
    # nested groups
    elif getattr(shape, "shape_type", None) == 6:  #grouped shape
        for sub_shape in shape.shapes:
            items.extend(_extract_shape_content(sub_shape))

    return items

def extract_pptx(file_bytes: bytes):
    prs = Presentation(BytesIO(file_bytes))
    pages = []

    for i, slide in enumerate(prs.slides, start=1):
        valid_shapes = []

        for shape in slide.shapes:
            valid_shapes.extend(_extract_shape_content(shape))

        valid_shapes.sort(key=lambda s: (s[0], s[1]))

        slide_text = "\n".join([item[2] for item in valid_shapes]).strip()

        pages.append(
            {"page": i, "text": slide_text, "is_virtual_page": False}
        )

    return pages

async def extract_image_ocr(file_bytes):
    # Decode image from input bytes
    np_arr = np.frombuffer(file_bytes, np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    if image is None:
        raise ValueError("Failed to process image!")

    ocr_reader = get_ocr_reader()
    ocr_results = await asyncio.to_thread(
        ocr_reader.readtext, image, detail=1
    )

    valid_words = []
    total_confidence = 0.0

    for bbox, text_str, confidence in ocr_results:
        clean_str = text_str.strip()
        if confidence > 0.35:
            valid_words.append(clean_str)
            total_confidence += confidence

    avg_confidence = (total_confidence / len(valid_words)) if valid_words else 0.0

    # Return EasyOCR output if text detection quality is sufficient
    if len(valid_words) >= 3 and avg_confidence >= 0.55:
        extracted_text = " ".join(valid_words)
        return [{"page": 1, "text": extracted_text, "source": "easyocr"}]

    # Fall back to Gemini Flash for messy or handwritten text
    pil_image = Image.open(BytesIO(file_bytes))

    prompt = (
        "Transcribe all handwritten and printed text in this image accurately. "
        "Return ONLY the transcribed text."
    )

    response = await asyncio.to_thread(
        lambda: get_ai_client().models.generate_content(
            model="gemini-3.8-flash", contents=[pil_image, prompt]
        )
    )
        
    return [
        {
            "page": 1,
            "text": response.text.strip(),
            "source": "gemini-flash"
        }
    ]


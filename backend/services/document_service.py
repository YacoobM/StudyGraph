import fitz
import os
from pathlib import Path
from docx import Document
from pptx import Presentation
from io import BytesIO
import easyocr
import cv2
import numpy as np
from PIL import Image
from google import genai
import io
import os
from dotenv import load_dotenv
from groq import RateLimitError
from services.llm_gateway import estimate_tokens

logger = logging.getLogger(__name__)

MAX_INPUT_TOKENS_PER_CALL = 4200
MAX_CHUNKS_PER_BATCH = 4


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
        except RateLimitError:
            logger.warning("extraction_batch_rate_limited batch=%s/%s attempt=%d/%d", batch_index, total_batches, attempt, retries)
            if attempt == retries:
                break
            await asyncio.sleep(delay)
            delay *= 2
    raise Exception("Maximum retries exceeded!")

load_dotenv()

# Initialize EasyOCR reader (better for handwriting than Paddle)
reader = easyocr.Reader(['en'], gpu=False)
_ai_client = None

def get_ai_client():
    global _ai_client
    if _ai_client is None:
        key = os.getenv("GEMINI_VISION_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_VISION_API_KEY is not set. It is only needed to read "
                "text out of images and handwriting — PDF, DOCX and PPTX "
                "files do not use it. Add the key to backend/.env to turn "
                "image OCR on."
            )
        _ai_client = genai.Client(api_key=key)
    return _ai_client

def extract_text(file_bytes, filename=None):
    suffix = Path(filename).suffix.lower()
    
    if suffix == ".pdf":
        return extract_pdf(file_bytes)

    elif suffix == ".docx":
        return extract_word_doc(file_bytes)

    elif suffix == ".pptx":
        return extract_pptx(file_bytes)

    elif suffix in [".txt", ".md"]:
        return [{"page": 1, "text": file_bytes.decode("utf-8-sig")}]

    elif suffix in [".png", ".jpg", ".jpeg"]:
        return extract_image_ocr(file_bytes)

    else:
        raise Exception("File is not supported!")
    

def extract_pdf(file_bytes):
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages = [{"page": i + 1, "text": p.get_text()} for i, p in enumerate(doc)]
    return pages

def extract_word_doc(file_bytes):
    document = Document(BytesIO(file_bytes))

    text = "\n".join(
        para.text
        for para in document.paragraphs
        if para.text.strip()
    )

    return [
        {
            "page": 1,
            "text": text
        }
    ]
        
def extract_pptx(file_bytes):
    prs = Presentation(BytesIO(file_bytes))

    pages = []

    for i, slide in enumerate(prs.slides, start=1):
        text = ""

        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                text += shape.text + "\n"

        pages.append({
            "page": i,
            "text": text.strip()
        })

    return pages


def extract_image_ocr(file_bytes):
    # Decode image from input bytes
    np_arr = np.frombuffer(file_bytes, np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    
    if image is None:
        raise ValueError("Failed to process image!")

    # Attempt local EasyOCR extraction
    ocr_results = reader.readtext(image, detail=1)
    
    valid_words = []
    total_confidence = 0.0
    
    for bbox, text_str, confidence in ocr_results:
        clean_str = text_str.strip()
        if len(clean_str) > 1 and confidence > 0.3:
            valid_words.append(clean_str)
            total_confidence += confidence
            
    avg_confidence = (total_confidence / len(valid_words)) if valid_words else 0.0

    # Return EasyOCR output if text detection quality is sufficient
    if len(valid_words) >= 3 and avg_confidence >= 0.55:
        extracted_text = " ".join(valid_words)
        return [
            {
                "page": 1,
                "text": extracted_text,
                "source": "easyocr"
            }
        ]

    # Fall back to Gemini Flash for messy or handwritten text
    pil_image = Image.open(io.BytesIO(file_bytes))
    
    prompt = (
        "Transcribe all handwritten and printed text in this image accurately. "
        "Return ONLY the transcribed text."
    )
    
    response = get_ai_client().models.generate_content(
        model='gemini-flash-latest',
        contents=[pil_image, prompt]
    )
        
    return [
        {
            "page": 1,
            "text": response.text.strip(),
            "source": "gemini-flash"
        }
    ]
import asyncio
import logging
import time

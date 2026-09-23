import asyncio
import cv2
import easyocr
import numpy as np
from io import BytesIO
from pathlib import Path
from PIL import Image
from docx import Document
from pptx import Presentation
from google import genai
import fitz
import os

from app.logging_config import get_logger

logger = get_logger(__name__)

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


async def extract_image_ocr(file_bytes):
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
    pil_image = Image.open(BytesIO(file_bytes))
    
    prompt = (
        "Transcribe all handwritten and printed text in this image accurately. "
        "Return ONLY the transcribed text."
    )


    response = await asyncio.to_thread(
        lambda: get_ai_client().models.generate_content(
            model='gemini-flash-latest',
            contents=[pil_image, prompt]
        )
    )
    
    return [
        {
            "page": 1,
            "text": response.text.strip(),
            "source": "gemini-flash"
        }
    ]
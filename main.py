from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright
from pdf2docx import Converter
from enum import Enum
import os
import base64
from pathlib import Path
import logging
import tempfile

# Logging configuratie
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="HTML to PDF/DOCX Converter",
    description="Convert HTML to PDF or Word (DOCX) with full CSS support",
    version="2.0.0"
)

# CORS configuratie voor n8n
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Output directory aanmaken
OUTPUT_DIR = Path("/app/static/output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Statische files hosten
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")


class OutputFormat(str, Enum):
    pdf = "pdf"
    docx = "docx"


class ConversionRequest(BaseModel):
    html: str = Field(..., description="Volledige HTML string om te converteren")
    filename: str = Field(..., description="Naam van het output bestand (bijv. cv_1234.pdf of document.docx)")
    output_format: OutputFormat = Field(OutputFormat.pdf, description="Output formaat: 'pdf' of 'docx'")
    return_base64: bool = Field(False, description="Optioneel: return bestand als base64 string")
    
    class Config:
        json_schema_extra = {
            "example": {
                "html": "<!DOCTYPE html><html><head><style>@page { margin: 2cm; }</style></head><body><h1>Test Document</h1></body></html>",
                "filename": "test.pdf",
                "output_format": "pdf",
                "return_base64": False
            }
        }


class ConversionResponse(BaseModel):
    url: str = Field(..., description="URL naar het gegenereerde bestand")
    base64: str | None = Field(None, description="Base64 encoded bestand (indien gevraagd)")
    size_kb: float = Field(..., description="Bestandsgrootte in KB")
    format: str = Field(..., description="Output formaat (pdf of docx)")


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "online",
        "service": "HTML to PDF Converter",
        "version": "1.0.0"
    }


@app.get("/health")
async def health():
    """Health check voor Render"""
    return {"status": "healthy"}


@app.post("/convert", response_model=ConversionResponse)
async def convert_html_to_pdf(request: ConversionRequest):
    """
    Converteer HTML naar PDF of DOCX
    
    - **PDF**: Gebruikt Chromium voor perfecte CSS rendering
    - **DOCX**: Genereert eerst PDF, converteert dan naar bewerkbaar Word document
    - Beide formaten gebruiken dezelfde rendering engine voor consistente output
    - UTF-8 encoding voor correcte karakters
    """
    try:
        # Bepaal correct extension
        file_extension = request.output_format.value
        
        # Valideer en corrigeer filename
        if not request.filename.endswith(f'.{file_extension}'):
            base_name = request.filename.rsplit('.', 1)[0] if '.' in request.filename else request.filename
            request.filename = f"{base_name}.{file_extension}"
        
        # Sanitize filename
        safe_filename = "".join(c for c in request.filename if c.isalnum() or c in ('_', '-', '.'))
        output_path = OUTPUT_DIR / safe_filename
        
        logger.info(f"Starting {request.output_format.upper()} conversion for: {safe_filename}")
        
        # Stap 1: Genereer altijd eerst een PDF (perfecte rendering)
        if request.output_format == OutputFormat.pdf:
            # Direct PDF output
            await generate_pdf(request.html, output_path)
        else:
            # Voor DOCX: eerst PDF maken, dan converteren
            temp_pdf = OUTPUT_DIR / f"temp_{safe_filename.replace('.docx', '.pdf')}"
            try:
                # Genereer PDF
                await generate_pdf(request.html, temp_pdf)
                
                # Converteer PDF naar DOCX
                await pdf_to_docx(temp_pdf, output_path)
                
            finally:
                # Cleanup temp PDF
                if temp_pdf.exists():
                    temp_pdf.unlink()
        
        logger.info(f"{request.output_format.upper()} successfully generated: {safe_filename}")
        
        # Bestandsgrootte bepalen
        file_size = output_path.stat().st_size / 1024  # in KB
        
        # Base URL bepalen (Render.com)
        base_url = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
        file_url = f"{base_url}/output/{safe_filename}"
        
        response_data = {
            "url": file_url,
            "size_kb": round(file_size, 2),
            "format": request.output_format.value
        }
        
        # Optioneel: base64 encoding
        if request.return_base64:
            with open(output_path, 'rb') as f:
                file_base64 = base64.b64encode(f.read()).decode('utf-8')
                response_data["base64"] = file_base64
        
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logger.error(f"Conversion error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Fout bij het converteren van HTML naar {request.output_format.upper()}: {str(e)}"
        )


async def generate_pdf(html: str, output_path: Path):
    """Genereer PDF met Playwright/Chromium"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--no-first-run',
                '--no-zygote',
                '--single-process',
                '--disable-extensions'
            ]
        )
        
        try:
            page = await browser.new_page()
            
            await page.set_content(
                html,
                wait_until='networkidle',
                timeout=30000
            )
            
            await page.pdf(
                path=str(output_path),
                format='A4',
                print_background=True,
                prefer_css_page_size=True,
                margin={
                    'top': '2cm',
                    'bottom': '2cm',
                    'left': '1.5cm',
                    'right': '1.5cm'
                },
                display_header_footer=False,
            )
            
        finally:
            await browser.close()


async def pdf_to_docx(pdf_path: Path, docx_path: Path):
    """Converteer PDF naar DOCX met pdf2docx"""
    try:
        # Converteer PDF naar DOCX
        cv = Converter(str(pdf_path))
        cv.convert(str(docx_path), start=0, end=None)
        cv.close()
        
        logger.info(f"PDF to DOCX conversion successful")
        
    except Exception as e:
        raise Exception(f"PDF naar DOCX conversie fout: {str(e)}")


@app.delete("/output/{filename}")
async def delete_pdf(filename: str):
    """Verwijder een gegenereerd PDF bestand"""
    try:
        file_path = OUTPUT_DIR / filename
        if file_path.exists():
            file_path.unlink()
            return {"message": f"Bestand {filename} succesvol verwijderd"}
        else:
            raise HTTPException(status_code=404, detail="Bestand niet gevonden")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

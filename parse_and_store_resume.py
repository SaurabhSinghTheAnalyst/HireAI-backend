import os
import json
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from supabase import create_client, Client
from dotenv import load_dotenv
import pdfplumber
import docx
from score_candidates import router as score_candidates_router
from fastapi.responses import JSONResponse
import requests
import re

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(score_candidates_router)

def extract_text_from_file(file: UploadFile) -> str:
    try:
        if file.filename.endswith(".pdf"):
            with pdfplumber.open(file.file) as pdf:
                text = "\n".join(page.extract_text() or '' for page in pdf.pages)
        elif file.filename.endswith(".docx"):
            doc = docx.Document(file.file)
            text = "\n".join([para.text for para in doc.paragraphs])
        elif file.filename.endswith(".txt"):
            text = file.file.read().decode("utf-8")
        else:
            raise HTTPException(status_code=400, detail="Unsupported file type.")
        if not text.strip():
            raise HTTPException(status_code=400, detail="Extracted text is empty.")
        print("Extracted text from file:", text)
        return text
    except Exception as e:
        print("Error extracting text from file:", str(e))
        raise HTTPException(status_code=400, detail=f"Failed to extract text from file: {str(e)}")

def parse_resume_with_openai(resume_text: str) -> dict:
    prompt = '''
    You are an AI bot designed to act as a professional for parsing resumes. You are given with resume and your job is to extract the following information from the resume:
    1. full name
    2. email id
    3. github portfolio
    4. linkedin id
    5. employment details (as a structured array of objects with keys: company, position, start_date, end_date, responsibilities)
    6. education history (as a structured array of objects with keys: degree, institution, start_date, end_date)
    7. technical skills
    8. soft skills
    Give the extracted information in json format only
    '''
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": resume_text}
    ]
    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.0,
            max_tokens=1500
        )
        content = response.choices[0].message.content
        print("OpenAI extracted:", content)
        if not content.strip():
            raise ValueError("LLM returned empty output.")
        try:
            extracted = json.loads(content)
        except json.JSONDecodeError:
            raise ValueError("LLM output is not valid JSON.")
        return extracted
    except Exception as e:
        print("Error parsing resume with OpenAI:", str(e))
        raise HTTPException(status_code=400, detail=f"Failed to parse resume with OpenAI: {str(e)}")

def safe_array(val):
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val.strip() == "":
        return []
    if val is None:
        return []
    # If it's a comma-separated string, split it
    if isinstance(val, str):
        return [v.strip() for v in val.split(",") if v.strip()]
    return []

@app.post("/parse-and-store-resume/")
async def parse_and_store_resume(
    user_id: str = Form(...),
    resume: UploadFile = File(...)
):
    try:
        text = extract_text_from_file(resume)
        extracted = parse_resume_with_openai(text)
        print("Extracted dict:", extracted)
        if not extracted:
            raise HTTPException(status_code=400, detail="No data extracted from resume.")
        data = {
            "user_id": user_id,
            "first_name": extracted.get("full_name", ""),
            "email": extracted.get("email", ""),
            "github_url": extracted.get("github", ""),
            "linkedin_url": extracted.get("linkedin", ""),
            "job_experience": extracted.get("employment_details", []),
            "education_history": extracted.get("education_history", []),
            "skills": safe_array(extracted.get("technical_skills")),
            "certifications": safe_array(extracted.get("certifications")),
            # Add more fields as needed
        }
        print("Data to upsert:", data)
        supabase.table("candidates").upsert(data, on_conflict="user_id").execute()
        return {"status": "success", "data": data}
    except Exception as e:
        print("Error in parse_and_store_resume:", str(e))
        return JSONResponse(status_code=400, content={"detail": f"Failed to parse resume: {str(e)}"})

@app.post("/generate-summary/")
async def generate_summary(parsed_data: dict):
    try:
        prompt = f"""
        You are an expert career coach. Given the following candidate information, write a concise, impactful professional summary suitable for a resume or LinkedIn profile. Use the candidate's skills, experience, and achievements to highlight their strengths.
        
        Candidate Information:
        {json.dumps(parsed_data, indent=2)}
        
        Professional Summary:
        """
        messages = [
            {"role": "system", "content": "You are a helpful assistant for resume writing."},
            {"role": "user", "content": prompt}
        ]
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.7,
            max_tokens=300
        )
        summary = response.choices[0].message.content.strip()
        return {"summary": summary}
    except Exception as e:
        return JSONResponse(status_code=400, content={"detail": f"Failed to generate summary: {str(e)}"})

@app.post("/save-candidate/")
async def save_candidate(candidate_data: dict = Body(...)):
    try:
        user_id = candidate_data.get("user_id")
        if not user_id:
            return JSONResponse(status_code=400, content={"detail": "user_id is required"})
        supabase.table("candidates").upsert(candidate_data, on_conflict="user_id").execute()
        return {"status": "success"}
    except Exception as e:
        return JSONResponse(status_code=400, content={"detail": f"Failed to save candidate: {str(e)}"})

def search_company_info(company_name):
    if not SERPER_API_KEY or not company_name:
        print(f"[Serper] Skipping web search: SERPER_API_KEY set? {bool(SERPER_API_KEY)}, company_name: '{company_name}'")
        return ""
    print(f"[Serper] Performing web search for company: '{company_name}'")
    url = "https://google.serper.dev/search"
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    data = {"q": f"{company_name} about us"}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        results = response.json()
        snippets = []
        for item in results.get("organic", []):
            if "snippet" in item:
                snippets.append(item["snippet"])
        print(f"[Serper] Top snippets: {snippets[:3]}")
        return "\n".join(snippets[:3])
    except Exception as e:
        print(f"[Serper] API error: {e}")
        return ""

def flatten_description(desc):
    if isinstance(desc, dict):
        sections = []
        for key, value in desc.items():
            if isinstance(value, list):
                value = "\n".join(f"- {item}" for item in value)
            sections.append(f"{key}:\n{value}\n")
        return "\n".join(sections)
    return str(desc)

@app.post("/generate-job-description/")
async def generate_job_description(payload: dict):
    try:
        prompt = payload.get("prompt", "")
        if not prompt:
            return JSONResponse(status_code=400, content={"detail": "Prompt is required"})
        # Try to extract company name from the prompt (simple heuristic)
        company_name = payload.get("company")
        if not company_name:
            import re
            match = re.search(r"at ([A-Za-z0-9 &]+)", prompt)
            company_name = match.group(1) if match else ""
        print(f"[JD Endpoint] Extracted company name: '{company_name}' from prompt.")
        # company_info = search_company_info(company_name)  # Web search integration left for future use
        system_prompt = '''
You are an expert HR and recruitment assistant. Given a job advertisement in natural language, extract and structure the information into a valid JSON object using the following fields:
- title: The full job title as seen in the ad.
- company: The company name (leave blank if not mentioned explicitly).
- location: The job's primary location (leave blank if not specified).
- type: The job type (e.g., Full-time, Part-time, Contract, Casual, Internship, or leave blank if not mentioned).
- description: Write a compelling, professional job description using the following structure:
  - Start with an "About the job" section, summarizing the company and the role in a friendly, human tone.
  - Add a "Why us?" section, highlighting company culture, benefits, and what makes the company unique.
  - Include a "Your Purpose" section, describing the impact and goals of the role.
  - Add a "Your Responsibilities" section as a bullet-point list, with each responsibility on a new line.
  - Add a "Your Expertise" section as a bullet-point list, with each requirement or skill on a new line.
  - End with an "Additional Information" section, including diversity, inclusion, and application encouragement statements if present.
  - Use line breaks and bullet points for clarity and readability.
- requirements: A single string of bullet-point style requirements, where each requirement is separated by a semicolon (;). Extract both technical and soft skills, tools, and qualifications listed.
- salary: Specify salary range if available, or return "Not specified".

Output only a valid JSON object. Do not include any extra explanation, preamble, or formatting.
'''
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.7,
            max_tokens=1200
        )
        content = response.choices[0].message.content
        print("LLM Job Description Output:", content)
        try:
            job_data = json.loads(content)
        except Exception:
            return JSONResponse(status_code=500, content={"detail": "Failed to parse LLM output as JSON"})
        # Flatten description if it's a dict
        if isinstance(job_data.get("description"), dict):
            job_data["description"] = flatten_description(job_data["description"])
        return {"jobData": job_data}
    except Exception as e:
        return JSONResponse(status_code=400, content={"detail": f"Failed to generate job description: {str(e)}"})

@app.post("/generate-linkedin-post/")
async def generate_linkedin_post(payload: dict):
    try:
        job_data = payload.get("jobData", {})
        prompt = f"""
        Write a compelling LinkedIn post to announce a job opening using the following details:
        Job Title: {job_data.get('title', '')}
        Company: {job_data.get('company', '')}
        Location: {job_data.get('location', '')}
        Description: {job_data.get('description', '')}
        Requirements: {job_data.get('requirements', '')}
        Salary: {job_data.get('salary', '')}
        The post should be engaging, encourage applications, and suitable for LinkedIn.
        """
        messages = [
            {"role": "system", "content": "You are a professional recruiter and social media expert."},
            {"role": "user", "content": prompt}
        ]
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.7,
            max_tokens=400
        )
        linkedin_post = response.choices[0].message.content.strip()
        print("LLM LinkedIn Post Output:", linkedin_post)
        return {"linkedinPost": linkedin_post}
    except Exception as e:
        return JSONResponse(status_code=400, content={"detail": f"Failed to generate LinkedIn post: {str(e)}"})

@app.post("/api/ai-candidate-search")
async def ai_candidate_search(payload: dict = Body(...)):
    prompt = payload.get("prompt", "")
    if not prompt:
        return JSONResponse(status_code=400, content={"detail": "Prompt is required"})
    # Fetch all candidates
    candidates_resp = supabase.table("candidates").select("*").execute()
    candidates = candidates_resp.data if hasattr(candidates_resp, 'data') else candidates_resp.get('data', [])
    if not candidates:
        return {"matches": []}
    # Prepare LLM prompt
    llm_prompt = f"""
You are an AI recruiter. Given the following job search prompt: \"{prompt}\", and the following candidate profiles:
{json.dumps(candidates, indent=2)}

Rank and return the best matching candidates as a JSON array. For each candidate, include:
- id
- first_name
- last_name
- email
- professional_summary
- skills
- match_score (0-100)
- reason (1-2 sentences why this candidate matches the prompt)
Return only the JSON array, no extra text.
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": llm_prompt}],
            max_tokens=2000,
            temperature=0.3,
        )
        content = response.choices[0].message.content
        print("AI Search LLM output:", content)
        # Remove code block markers if present
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
        matches = json.loads(content)
        return {"matches": matches}
    except Exception as e:
        print("AI candidate search error:", str(e))
        return JSONResponse(status_code=500, content={"detail": f"AI search failed: {str(e)}"}) 
print("[DEBUG] score_candidates.py loaded")
import os
import openai
from fastapi import APIRouter, Body
import re
import json
from dotenv import load_dotenv

# Try to use MCP for Supabase if available
try:
    from supabase_mcp import create_client as mcp_create_client
    USE_MCP = True
except ImportError:
    USE_MCP = False
    from supabase import create_client, Client

router = APIRouter()

load_dotenv()

openai.api_key = os.getenv("OPENAI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

print(f"[DEBUG] Using Supabase key: {SUPABASE_SERVICE_ROLE_KEY[:8]}... (length: {len(SUPABASE_SERVICE_ROLE_KEY)})")

if USE_MCP:
    print("[INFO] Using MCP for Supabase client.")
    supabase = mcp_create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
else:
    print("[INFO] Using standard Supabase client.")
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

@router.post("/api/score-candidates")
async def score_candidates(
    job: dict = Body(...),
    candidates: list = Body(...)
):
    print("Received request to /api/score-candidates")
    print("Job:", job)
    print("Number of candidates:", len(candidates))
    results = []
    for idx, candidate in enumerate(candidates):
        print(f"Scoring candidate {idx+1}/{len(candidates)}: {candidate.get('first_name', '')} {candidate.get('last_name', '')} (ID: {candidate.get('id')})")
        prompt = f"""
You are an expert technical recruiter. Given the following job description and requirements:

Job Description: {job.get('description', '')}
Requirements: {job.get('requirements', '')}

And the following candidate profile:
Name: {candidate.get('first_name', '')} {candidate.get('last_name', '')}
Email: {candidate.get('email', '')}
Skills: {', '.join(candidate.get('skills', []))}
Experience: {candidate.get('job_experience', '')}
Education: {candidate.get('education_history', '')}

Score this candidate from 1-10 for fit to the job, and explain your reasoning in 2-3 sentences.
Return your answer in the format: SCORE: <number> | REASON: <reason>
"""
        print("Prompt sent to OpenAI:\n", prompt)
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200
        )
        score_text = response.choices[0].message.content
        print(f"OpenAI response for candidate {candidate.get('id')}: {score_text}")
        print("[DEBUG] After LLM response, before parsing and upsert")
        try:
            print("[DEBUG] Entered try block for parsing and upsert")
            score_match = re.search(r"SCORE:\s*(\d+)", score_text)
            reason_match = re.search(r"REASON:\s*(.*)", score_text)
            score_val = int(score_match.group(1)) if score_match else None
            reason_val = reason_match.group(1).strip() if reason_match else None

            print(f"[DEBUG] Full candidate dict: {candidate}")
            job_id = candidate.get('job_id')
            candidate_id = candidate.get('candidate_id')
            application_id = candidate.get('application_id')
            print(f"[DEBUG] job_id before update: {job_id}, candidate_id before update: {candidate_id}, score_val: {score_val}")

            if not job_id or not candidate_id:
                print(f"[ERROR] job_id or candidate_id missing. Skipping update.")
            elif score_val is None:
                print(f"[ERROR] score_val is None. Skipping update.")
            elif not application_id:
                print("[ERROR] application_id missing. Skipping update.")
            else:
                try:
                    print(f"[DEBUG] Attempting update by application_id: {application_id}")
                    update_result = supabase.table("applications").update({
                        "llm_score": score_val,
                        "llm_evaluation": reason_val
                    }).eq("id", application_id).execute()
                    print(f"[DEBUG] Update result by application_id: {update_result}")
                    if hasattr(update_result, 'error') and update_result.error:
                        print(f"[DEBUG] Update by application_id error: {update_result.error}")
                    if hasattr(update_result, 'data') and (not update_result.data or len(update_result.data) == 0):
                        print(f"[WARNING] Update by application_id did not affect any rows.")
                except Exception as e:
                    print(f"[DEBUG] Exception during update by application_id: {e}")
        except Exception as e:
            print(f"[EXCEPTION] Exception caught: {e}")
        results.append({
            "candidate_id": candidate_id,
            "score": score_val,
            "reason": reason_val
        })
    print("Scoring complete. Returning results.")
    return results 
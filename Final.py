# ==============================================================================
# B.E RESEARCH INVESTING ASSISTANT - MASTER CODEBASE
# ==============================================================================

# --- IMPORTS ---
import streamlit as st            
import yfinance as yf             
import concurrent.futures         
import io                         
import zipfile                    
import sqlite3                    
import pandas as pd               
import smtplib                    
import time                       
import markdown                   
import requests                   
import json                       
import ast                        
import re   
import matplotlib.pyplot as plt
import base64
from email.mime.multipart import MIMEMultipart 
from email.mime.base import MIMEBase           
from email.mime.text import MIMEText           
from email import encoders                     
from datetime import datetime, timedelta       
from google import genai                       
from google.genai import types  
from xhtml2pdf import pisa

# --- 0. SUPER USERS ---
SUPER_USERS = ["boatengampomah@gmail.com", "emcheix@gmail.com"]

# ==============================================================================
# --- 1. SELF-HEALING DATABASE, QUOTAS, & DOSSIER LOGIC ---
# ==============================================================================

# ==============================================================================
# --- 1. SELF-HEALING DATABASE, QUOTAS, & DOSSIER LOGIC ---
# ==============================================================================

def init_db():
    try:
        conn = sqlite3.connect("users.db")
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, target_ticker TEXT, timestamp TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS usage_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, run_timestamp TEXT, is_premium BOOLEAN, report_count INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS alerts (email TEXT, alert_type TEXT, timestamp TEXT)""")
        
        # --- NEW: SUBSCRIPTION TABLE ---
        c.execute("""CREATE TABLE IF NOT EXISTS subscriptions (email TEXT PRIMARY KEY, tier TEXT)""")
        
        c.execute("""CREATE TABLE IF NOT EXISTS dossiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT, ticker TEXT, business_summary TEXT, moat_notes TEXT, management_notes TEXT,
            key_metrics TEXT, thesis TEXT, anti_thesis TEXT, valuation_assumptions TEXT,
            watchlist_triggers TEXT, last_updated TEXT, UNIQUE(email, ticker)
        )""")
        try: c.execute("ALTER TABLE dossiers ADD COLUMN scorecard TEXT")
        except Exception: pass 
            
        conn.commit(); conn.close()
    except Exception: pass

# --- NEW: TIER MANAGEMENT FUNCTIONS ---
def get_user_tier(email):
    if not email: return "Free"
    if email.lower() in SUPER_USERS: return "Ultra"
    init_db()
    try:
        conn = sqlite3.connect("users.db"); c = conn.cursor()
        c.execute("SELECT tier FROM subscriptions WHERE email=?", (email.lower(),))
        row = c.fetchone()
        conn.close()
        if row: return row[0]
        return "Free" # Default if not found
    except Exception: return "Free"

def set_user_tier(email, tier):
    init_db()
    try:
        conn = sqlite3.connect("users.db"); c = conn.cursor()
        c.execute("INSERT INTO subscriptions (email, tier) VALUES (?, ?) ON CONFLICT(email) DO UPDATE SET tier=excluded.tier", (email.lower(), tier))
        conn.commit(); conn.close()
    except Exception: pass

def save_lead(email, ticker):
    init_db()
    try:
        conn = sqlite3.connect("users.db"); c = conn.cursor()
        c.execute("INSERT INTO leads (email, target_ticker, timestamp) VALUES (?, ?, ?)", (email, ticker, datetime.now().isoformat()))
        conn.commit(); conn.close()
    except Exception: pass

def get_usage(email):
    init_db()
    try:
        conn = sqlite3.connect("users.db"); c = conn.cursor()
        forty_eight_hours_ago = (datetime.now() - timedelta(hours=48)).isoformat()
        c.execute("SELECT is_premium, report_count FROM usage_logs WHERE email=? AND run_timestamp >= ?", (email, forty_eight_hours_ago))
        rows = c.fetchall(); conn.close()
        p_runs, p_reps, s_reps = 0, 0, 0
        for is_premium, count in rows:
            if is_premium: p_runs += 1; p_reps += count
            else: s_reps += count
        return p_runs, p_reps, s_reps
    except Exception: return 0, 0, 0

def log_usage(email, is_premium, report_count):
    init_db()
    try:
        conn = sqlite3.connect("users.db"); c = conn.cursor()
        c.execute("INSERT INTO usage_logs (email, run_timestamp, is_premium, report_count) VALUES (?, ?, ?, ?)", (email, datetime.now().isoformat(), is_premium, report_count))
        conn.commit(); conn.close()
    except Exception: pass

def send_limit_email(email, limit_msg):
    init_db()
    try:
        conn = sqlite3.connect("users.db"); c = conn.cursor()
        twenty_four_hours_ago = (datetime.now() - timedelta(hours=24)).isoformat()
        c.execute("SELECT COUNT(*) FROM alerts WHERE email=? AND alert_type='limit' AND timestamp >= ?", (email, twenty_four_hours_ago))
        if c.fetchone()[0] == 0:
            try:
                msg = MIMEMultipart()
                msg["From"] = f"B.E Research <{st.secrets['EMAIL_SENDER']}>"
                msg["To"] = email
                msg["Subject"] = "Action Required: B.E Research Usage Limit Reached"
                body = f"Hello,\n\nYou have reached a usage limit on the platform.\n\nDETAIL: {limit_msg}\n\nPlease wait until your 48-hour rolling window resets.\n\nBest,\nB.E Research Team"
                msg.attach(MIMEText(body, "plain"))
                server = smtplib.SMTP("smtp.gmail.com", 587); server.starttls(); server.login(st.secrets["EMAIL_SENDER"], st.secrets["EMAIL_PASSWORD"]); server.send_message(msg); server.quit()
                c.execute("INSERT INTO alerts (email, alert_type, timestamp) VALUES (?, ?, ?)", (email, "limit", datetime.now().isoformat())); conn.commit()
            except Exception: pass
        conn.close()
    except Exception: pass

def save_dossier(email, ticker, data_dict, scorecard_dict):
    init_db()
    try:
        conn = sqlite3.connect("users.db"); c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        def make_safe_string(val):
            if isinstance(val, (dict, list)): return json.dumps(val, indent=2)
            return str(val) if val else "N/A"
            
        bs = make_safe_string(data_dict.get("business_summary", "N/A"))
        mn = make_safe_string(data_dict.get("moat_notes", "N/A"))
        mgn = make_safe_string(data_dict.get("management_notes", "N/A"))
        km = make_safe_string(data_dict.get("key_metrics", "N/A"))
        th = make_safe_string(data_dict.get("thesis", "N/A"))
        ath = make_safe_string(data_dict.get("anti_thesis", "N/A"))
        va = make_safe_string(data_dict.get("valuation_assumptions", "N/A"))
        wt = make_safe_string(data_dict.get("watchlist_triggers", "N/A"))
        
        sc_json = json.dumps(scorecard_dict) if scorecard_dict else "{}"
        
        sql = """
        INSERT INTO dossiers (email, ticker, business_summary, moat_notes, management_notes, key_metrics, thesis, anti_thesis, valuation_assumptions, watchlist_triggers, scorecard, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email, ticker) DO UPDATE SET
            business_summary=excluded.business_summary, moat_notes=excluded.moat_notes,
            management_notes=excluded.management_notes, key_metrics=excluded.key_metrics,
            thesis=excluded.thesis, anti_thesis=excluded.anti_thesis,
            valuation_assumptions=excluded.valuation_assumptions,
            watchlist_triggers=excluded.watchlist_triggers, 
            scorecard=excluded.scorecard,
            last_updated=excluded.last_updated;
        """
        c.execute(sql, (email, ticker, bs, mn, mgn, km, th, ath, va, wt, sc_json, now))
        conn.commit(); conn.close()
    except Exception as e: print(f"Dossier save error: {e}")

def get_user_dossiers(email):
    init_db()
    try:
        conn = sqlite3.connect("users.db")
        df = pd.read_sql_query("SELECT * FROM dossiers WHERE email=?", conn, params=(email,))
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

# ==============================================================================
# --- 2. SET UP THE WEB PAGE & HELPER FUNCTIONS ---
# ==============================================================================
st.set_page_config(page_title="B.E Research Investing Assistant", page_icon="📈", layout="wide")

@st.cache_resource
def get_task_registry(): return {}
@st.cache_resource
def get_executor(): return concurrent.futures.ThreadPoolExecutor(max_workers=2)

global_tasks = get_task_registry()
background_executor = get_executor()

if "final_reports" not in st.session_state: st.session_state.final_reports = {}
if "analysis_complete" not in st.session_state: st.session_state.analysis_complete = False
if "ticker_input" not in st.session_state: st.session_state.ticker_input = ""
if "company_input" not in st.session_state: st.session_state.company_input = ""
if "ceo_input" not in st.session_state: st.session_state.ceo_input = ""
if "concept_input" not in st.session_state: st.session_state.concept_input = ""
if "industry_input" not in st.session_state: st.session_state.industry_input = ""
if "market_pulse_data" not in st.session_state: st.session_state.market_pulse_data = "" 

def estimate_total_seconds(report_count, brain_id, tool_id, generate_audio):
    base_seconds = 30 
    if tool_id == "Deep Research": per_report = 120
    elif tool_id in ("Yahoo Finance Data", "Market Data"): per_report = 18
    else: per_report = 35
    if brain_id == "gemini-3.1-pro-preview": per_report += 20
    total = max(45, base_seconds + (report_count * per_report))
    if generate_audio: total += 60 
    return total

def format_eta(seconds_remaining):
    seconds_remaining = max(0, int(seconds_remaining))
    mins, secs = divmod(seconds_remaining, 60)
    if mins == 0: return f"{secs}s"
    return f"{mins}m {secs}s"

def update_task_progress(email, pct, detail):
    if email in global_tasks:
        global_tasks[email]["progress_pct"] = max(0.0, min(1.0, pct))
        global_tasks[email]["progress"] = detail

def fetch_info_from_ticker():
    ticker = st.session_state.ticker_input.strip().upper()
    st.session_state.ticker_input = ticker
    if not ticker: return
    company_name = ""
    ceo_name = ""
    
    try:
        stock = yf.Ticker(ticker); info = stock.info
        company_name = info.get("longName") or info.get("shortName") or ""
        for officer in info.get("companyOfficers", []):
            title = str(officer.get("title", "")).upper()
            if "CEO" in title or "CHIEF EXECUTIVE" in title:
                ceo_name = officer.get("name", ""); break
    except Exception: pass
        
    if not company_name or not ceo_name:
        try:
            if "GOOGLE_API_KEY" in st.secrets:
                client = genai.Client(api_key=st.secrets["GOOGLE_API_KEY"])
                prompt = f"What is the official Company Name and the current CEO's name for the stock ticker '{ticker}'? Return EXACTLY a JSON format: {{\"company_name\": \"Name\", \"ceo_name\": \"Name\"}}"
                res = client.models.generate_content(model="gemini-3.1-flash-lite-preview", contents=prompt)
                raw_json = res.text.strip().replace("```json", "").replace("```", "").strip()
                parsed = json.loads(raw_json)
                if not company_name: company_name = parsed.get("company_name", "")
                if not ceo_name: ceo_name = parsed.get("ceo_name", "")
        except Exception: pass

    if company_name: st.session_state.company_input = company_name
    if ceo_name: st.session_state.ceo_input = ceo_name

def generate_elevenlabs_audio(text, voice_id, api_key):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"Accept": "audio/mpeg", "Content-Type": "application/json", "xi-api-key": api_key}
    data = {"text": text, "model_id": "eleven_multilingual_v2", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    response = requests.post(url, json=data, headers=headers)
    if response.status_code == 200: return response.content
    else: raise Exception(f"API Error {response.status_code}: {response.text}")

@st.cache_data(ttl=3600) 
def get_live_trending_tickers(api_key):
    try:
        client = genai.Client(api_key=api_key)
        prompt = """Search X (Twitter) financial trends, Yahoo Finance, and Google News right now. 
        Identify the Top 5 most trending, bought, sold, or heavily researched stock tickers today.
        Respond ONLY with a valid Python list of 5 ticker symbols as strings. No markdown, no explanations.
        Example: ["NVDA", "TSLA", "AAPL", "PLTR", "MSTR"]"""
        res = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview", contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1, tools=[types.Tool(google_search=types.GoogleSearch())])
        )
        match = re.search(r'\[.*?\]', res.text)
        if match:
            tickers = ast.literal_eval(match.group(0))
            if isinstance(tickers, list) and len(tickers) > 0: return tickers[:5] 
        return ["NVDA", "PLTR", "TSLA", "AAPL", "MSFT"] 
    except Exception: return ["NVDA", "PLTR", "TSLA", "AAPL", "MSFT"] 

def fetch_trending_market_pulse(api_key):
    client = genai.Client(api_key=api_key)
    prompt = """You are an expert Wall Street market analyst. Search the live internet right now, specifically looking at X (Twitter) financial trends, Google Search trends, and Yahoo Finance.
    Identify and list the following:
    1. Top 5 Trending Stocks overall right now.
    2. Top 5 Recommended Buys currently being discussed by analysts.
    3. Top 5 Recommended Sells or Heavily Shorted stocks.
    4. Top 5 Most Researched/Searched stocks today.
    RULES: Format the output as a clean, highly readable Markdown report. Use bullet points, bold the Ticker Symbol, and include a 1-sentence reason why it is on the list based on current news or social media chatter."""
    try:
        res = client.models.generate_content(
            model="gemini-3.1-pro-preview", contents=prompt, 
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
        )
        return res.text
    except Exception as e: return f"Could not fetch trending stocks at this moment. (Error: {str(e)})"
def generate_financial_chart_base64(ticker):
    """Fetches 4 years of live data and generates a sleek, base64 encoded Matplotlib chart."""
    try:
        stock = yf.Ticker(ticker)
        fin = stock.financials
        if fin.empty: return None
        
        # Get the last 4 years and reverse for chronological order
        dates = fin.columns[:4][::-1]
        
        # Safely extract Revenue and Net Income in Billions
        revs = [fin.loc['Total Revenue', d]/1e9 if 'Total Revenue' in fin.index else 0 for d in dates]
        net_incomes = [fin.loc['Net Income', d]/1e9 if 'Net Income' in fin.index else 0 for d in dates]
        years = [str(d.year) for d in dates]

        # --- STYLE THE CHART TO LOOK LIKE A PREMIUM HEDGE FUND DECK ---
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(8, 4.5))
        
        x = range(len(years))
        width = 0.35
        
        # Draw the bars (Blue for Revenue, Green for Profit)
        ax.bar([i - width/2 for i in x], revs, width=width, label='Revenue ($B)', color='#1f77b4', edgecolor='white', linewidth=0.5)
        ax.bar([i + width/2 for i in x], net_incomes, width=width, label='Net Income ($B)', color='#2ca02c', edgecolor='white', linewidth=0.5)
        
        # Formatting
        ax.set_xticks(x)
        ax.set_xticklabels(years, fontsize=10, fontweight='bold')
        ax.set_title(f"{ticker.upper()} - 4 Year Financial Trajectory", fontsize=14, fontweight='bold', pad=15)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.yaxis.grid(True, linestyle='--', alpha=0.3)
        ax.legend(loc='upper left', frameon=False)
        
        plt.tight_layout()

        # Save to a bytes buffer and encode to Base64
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=200, bbox_inches='tight', transparent=True)
        plt.close(fig)
        
        return base64.b64encode(buf.getvalue()).decode('utf-8')
    except Exception as e:
        print(f"Chart generation failed: {e}")
        return None
def display_ui_scorecard(scorecard_data):
    if not scorecard_data or not isinstance(scorecard_data, dict):
        return
        
    st.markdown("### 📊 Transparent Scorecard")
    st.caption("A data-driven, transparent scoring framework out of 10. *We show our math.*")
    
    for category, details in scorecard_data.items():
        if isinstance(details, dict):
            try: score_val = float(details.get("score", 0))
            except ValueError: score_val = 0.0
            
            score_val = max(0.0, min(score_val, 10.0))
            
            st.markdown(f"**{category}: {score_val}/10**")
            st.progress(score_val / 10.0)
            
            c1, c2 = st.columns([1, 2])
            c1.markdown(f"**Confidence:** {details.get('confidence', 'N/A')}")
            c2.markdown(f"**Metrics Evaluated:** {details.get('metrics', 'N/A')}")
            st.markdown(f"> **Why:** {details.get('why', 'N/A')}")
            st.markdown("---")

# ==============================================================================
# --- 4. INSTITUTIONAL PROMPT LIBRARY ---
# ==============================================================================
gem_prompts = {
    # --- DEPENDENT AGENTS (SYNTHESIS) ---
    "Master Synthesis - The Institutional Tear Sheet": """ROLE: Director of Research at a Tier-1 Long/Short Institutional Equity Fund.
TASK: Synthesize the 10 provided forensic sub-reports into a flagship "Initiation of Coverage" Master Tear Sheet for [Company_name] ([TICKER]).

CRITICAL INSTRUCTIONS ON TONE & FORMATTING:
1. ABSOLUTE OBJECTIVITY: Do NOT use sensationalist language (e.g., "terrifying", "catastrophic", "death spiral"). Use cold, probabilistic, institutional terminology.
2. NO WALLS OF TEXT: You are strictly forbidden from writing paragraphs longer than 3 sentences. You MUST use bullet points heavily. 
3. BALANCED THESIS: You must accurately reflect both the Bull Case (from the Investment Memo) and the Bear Case (from the Forensic/Pre-Mortem reports). Do not let one overpower the other.
4. PROFESSIONAL STYLING: Do NOT use emojis, ASCII art, or fake text-based sliders. Use clean Markdown headers, bold text for key metrics, and crisp lists.

FORMAT EXACTLY TO THIS STRUCTURE:

# INITIATION OF COVERAGE: [COMPANY NAME] ([TICKER])
**Sector:** [Extract] | **Date:** Current

---
## 1. EXECUTIVE SUMMARY & INVESTMENT RATING
* **Consensus Rating:** [Synthesize the final rating based on the sub-reports]
* **The Core Thesis:** [Provide a 3-bullet summary of the overarching narrative and market mispricing].
* **Near-Term Catalysts:** [List 2 specific upcoming events that will move the stock].

## 2. THE BULL CASE (Growth & Moat)
* **The Economic Flywheel:** [Summarize how their ecosystem feeds itself, extracting from the Business Model report].
* **Competitive Moat (7 Powers):** [Provide 3 bullet points detailing their strongest Hamilton Helmer powers].
* **Earnings Call Sentiment:** [Summarize management's current tone and confidence].

## 3. THE BEAR CASE (Risks & Forensics)
* **Forensic Red Flags:** [List the top 2-3 accounting or working capital concerns found in the Forensic report].
* **Macro Sensitivities:** [List the specific macro, rate, or FX vulnerabilities].
* **The Terminal Risk:** [Provide a 2-bullet summary of the exact scenario that breaks the company's business model over the next 5 years].

## 4. FINANCIAL TRAJECTORY & UNIT ECONOMICS
* **Revenue Quality:** [Break down organic volume growth vs. inflationary pricing or M&A].
* **Margin & Capital Efficiency:** [Detail the ROIC trajectory and operating margin trends. Explain WHY margins are compressing or expanding].
* **The Fortress Test:** [Provide 2 bullet points on balance sheet health, debt walls, and true free cash flow generation].

## 5. MANAGEMENT & GOVERNANCE
* **Capital Allocation:** [Grade the CEO's track record of deploying capital/M&A].
* **Alignment:** [Note insider ownership levels and compensation metrics].

## 6. FINAL RESEARCH CONCLUSION
[Provide a final, 3-sentence objective conclusion weighing the asymmetric risk/reward of the equity at current valuations.]""",

    "Company - Final Investment Memo & Rating": """ROLE: You are the Lead Portfolio Manager and Senior Equity Analyst covering [STOCK NAME] ([TICKER]).
You are synthesizing all provided research into a final, actionable investment memo. 
CRITICAL RULES:
- Base your analysis ONLY on the provided research context and market data.
- Never present inferences as facts. Mark interpretations clearly.
- Be decisive. Hedge funds do not pay for "on the one hand, on the other hand" analysis. Take a stance.
OUTPUT STRUCTURE:
1. INVESTMENT RATING & PRICE ACTION: Give a definitive rating: [STRONG BUY / BUY / HOLD / SELL / AVOID]. Time Horizon: 12-24 Months. One-Sentence Core Thesis.
2. THE CORE THESIS (3 Bullet Points): What is the market currently misunderstanding or mispricing about [STOCK NAME]? What is the structural competitive advantage (Moat)?
3. NEAR-TERM CATALYSTS: Identify 2-3 specific events in the next 6-12 months that will force the stock price to move.
4. STRUCTURAL RISKS (The Pre-Mortem): If this investment completely fails in 3 years, what is the most likely reason why?
5. VALUATION PROXY: Based on the data provided, is the stock trading at a premium, discount, or fair value? Does the growth justify the multiple?
6. FINAL VERDICT: A concluding paragraph summarizing the risk/reward asymmetry.""",

    # --- INDUSTRY AGENTS ---
    "Industry - Macro Environment & Strategic Outlook": """ROLE: Senior Macro Strategist at a Global Macro Hedge Fund.
TASK: Produce a data-driven sector intelligence report for [INSERT INDUSTRY]. Focus on regime changes, capital flows, and structural shifts, not just generic trends.
OUTPUT STRUCTURE:
1. THE MACRO REGIME: How do current interest rates, inflation expectations, and liquidity cycles specifically aid or choke this industry?
2. STRUCTURAL TAILWINDS & HEADWINDS: Identify demographic, regulatory, and deglobalization forces. Which are structural (permanent) vs. cyclical (temporary)?
3. PROFIT POOL MIGRATION: Where is the economic value shifting globally? Which geographic regions are gaining leverage?
4. SUPPLY CHAIN FRAGILITY: Analyze input dependencies, geopolitical bottlenecks, and reshoring impacts.
5. STRATEGIC ALLOCATION: Base Case, Bull Case, and Bear Case for the next 5 years. What is the explicit catalyst that shifts the sector from Base to Bear?
RULES: Use bullet points. Be explicitly quantitative where possible. Avoid generic corporate jargon.""",

    "Industry - Future Growth & Disruption Scenarios": """ROLE: Innovation & Thematic Analyst (ARK Invest style).
TASK: Project the 5-10 year future for [Industry Name], focusing on Wright's Law (cost declines), technological convergence, and TAM expansion.
OUTPUT STRUCTURE:
1. STRUCTURAL GROWTH DRIVERS: What is the primary catalyst driving adoption? (e.g., cost curve crossing parity with legacy tech, regulatory mandate).
2. COST DECLINES & ADOPTION S-CURVES: Are core inputs getting cheaper? When does this hit mass market affordability?
3. VALUE DESTRUCTION: Which legacy industries or incumbents will have their profit pools destroyed by this growth?
4. EMERGING BUSINESS MODELS: How will companies monetize this? (Hardware sales, recurring software, data licensing).
5. THE 10-YEAR SCENARIO: Base, Bull, and Bear cases with assigned probabilities. Identify the single most important leading indicator to track to know which scenario is unfolding.""",

    "Industry - Core Economics & Market Structure": """ROLE: Top-Tier Management Consultant (McKinsey/Bain style).
TASK: Provide a masterclass overview of the [Insert Industry Name] industry. Focus on unit economics and value creation.
OUTPUT STRUCTURE:
1. CORE ECONOMIC ENGINE: What is the fundamental customer problem, and how exactly does the industry monetize the solution?
2. PROFIT POOL CONCENTRATION: Where are the highest margins made? (e.g., Manufacturers? Distributors? Software providers?). 
3. BARRIERS TO ENTRY: What actually stops a well-funded startup from taking 10% market share tomorrow?
4. SUPPLY & COST CONSTRAINTS: What are the primary fixed and variable costs? Is the industry highly sensitive to commodity prices or labor shortages?
5. CONSOLIDATION STAGE: Is this a fragmented market ripe for roll-ups, or an entrenched oligopoly?
6. INVESTOR SYNTHESIS: Provide 4 bullet points detailing what makes a "winner" in this space.""",

    "Industry - Business Models & Ecosystem Architecture": """ROLE: Senior Sector Analyst.
TASK: Decode the underlying business architecture and power dynamics of [INSERT INDUSTRY NAME].
OUTPUT STRUCTURE:
1. THE ECOSYSTEM MAP: Who are the raw material providers, the core operators, the distributors, and the end-users?
2. THE CHOKEPOINTS: Who holds the ultimate leverage in this industry? (e.g., ASML in semiconductors, Visa/Mastercard in payments). Who is most easily squeezed?
3. DOMINANT BUSINESS MODELS: Compare the incumbent model vs. the most dangerous disruptive model currently gaining traction.
4. THE ENABLERS: What secondary industries or tools are absolute prerequisites for this industry to function? 
5. KEY PERFORMANCE INDICATORS (KPIs): Define the 3 non-standard metrics that truly separate top-quartile operators from the rest.""",

    "Industry - Value Chain Mapping & Key Players": """ROLE: Hedge Fund Portfolio Manager.
TASK: Map the [Industry Name] value chain using 5-10 publicly traded companies.
OUTPUT STRUCTURE:
1. VALUE CHAIN OVERVIEW: Briefly describe the flow of value from upstream (raw/components) to downstream (end consumer).
2. THE PLAYERS (List 5-10): For each company, provide:
   - Name & Ticker
   - Exact role in the value chain.
   - Why they were selected (e.g., "Monopoly in upstream components", "Largest downstream distributor").
   - Estimated pricing power/leverage over the rest of the chain.
3. PROFITABILITY SKEW: Explicitly state which part of this value chain captures the highest Return on Capital, and why investors should care.""",

    "Industry - Unit Economics & Operating Leverage": """ROLE: Operations & Supply Chain Analyst.
TASK: Deconstruct the operational mechanics and unit economics of [Industry Name].
OUTPUT STRUCTURE:
1. UNIT ECONOMICS: How exactly is a dollar of revenue generated and consumed? Break down fixed vs. variable costs.
2. OPERATING LEVERAGE: Does margin expand aggressively as revenue grows, or do costs scale linearly with sales? 
3. CASH CONVERSION CYCLE: Do operators get paid before they deliver (negative working capital), or do they float inventory for months?
4. CAPEX INTENSITY: Is growth capital-intensive (building factories) or capital-light (software/IP)? What is the typical asset turnover?
5. VULNERABILITIES: What is the single point of failure in operations? (e.g., union labor strikes, specific rare earth metals, logistics bottlenecks).
6. THE TOP-QUARTILE OPERATOR: What specific operational decisions allow the best companies in this space to generate superior margins?""",

    "Industry - Geopolitics, Regulation & TAM": """ROLE: Geopolitical and Strategic Analyst.
TASK: Map the structural macro dynamics for [INSERT INDUSTRY NAME].
OUTPUT STRUCTURE:
1. TAM & GROWTH VELOCITY: Real current market size and 5-year CAGR expectations. Is the TAM actually expanding, or is it a zero-sum battle for market share?
2. GEOPOLITICAL EXPOSURE: How vulnerable is this industry to tariffs, trade wars, or national security mandates? 
3. REGULATORY CAPTURE: Is this industry heavily regulated? Does regulation act as a moat protecting incumbents, or a headwind destroying margins?
4. SUBSTITUTION RISK: What happens if a parallel technology or industry suddenly shifts? (e.g., How streaming destroyed physical media).
5. MILESTONE TIMELINE: List 3 historical events that permanently altered this industry, and project 1 future event that could disrupt it again.""",

    # --- CONCEPT AGENTS ---
    "Concept - Investment Education & Metric Breakdown": """ROLE: Director of Research training incoming Hedge Fund Analysts.
TASK: Deconstruct the concept of {CONCEPT NAME} for a smart investor.
OUTPUT STRUCTURE:
1. THE NAKED TRUTH: A 1-sentence, jargon-free definition.
2. THE MECHANICS: 3-5 bullet points on exactly how this actually works in the real world.
3. WHY WALL STREET CARES: How does this concept directly impact valuation, risk, or cash flow?
4. THE BULLSHIT TEST: What is the most common way management teams or sell-side analysts manipulate or misuse this metric to hide bad performance?
5. REAL-WORLD APPLICATION: A concrete example of this concept in action (e.g., how it looks on a 10-K or earnings call).
6. 3 METRICS TO CROSS-REFERENCE: What other data points must you check to ensure this concept isn't painting a false picture?""",

    # --- CEO AGENTS ---
    "CEO - Track Record & Capital Allocation": """ROLE: Institutional Activist Investor.
TASK: Produce a ruthless, evidence-based dossier on {{CEO Name}} at {{Company Name}}.
OUTPUT STRUCTURE:
1. ARCHETYPE: Is this CEO a Founder/Visionary, a Turnaround Operator, an Empire Builder, or a Bureaucratic Manager? 
2. TRACK RECORD OF DECISIONS: Evaluate their biggest capital allocation moves (M&A, massive CapEx, buybacks). Did they create or destroy Return on Invested Capital (ROIC)?
3. PROMISES VS. EXECUTION: Look at past guidance. Do they consistently over-promise and under-deliver? Do they move the goalposts on KPIs?
4. ALIGNMENT & INSIDER ACTION: Do they own a massive, unhedged stake in the stock, or are they a hired gun selling their RSUs the moment they vest?
5. INTEGRITY: How do they handle bad news on earnings calls? Do they take responsibility or blame external factors?
VERDICT: Is this CEO a compounder of capital or a risk to the thesis?""",

    # --- STOCK BASE AGENTS ---
    "Company - Management Quality & Insider Incentives": """ROLE: Activist Investor / Corporate Governance Analyst.
TASK: Perform a ruthless evaluation of [Company_name]'s management alignment with minority shareholders.
OUTPUT STRUCTURE: Render a verdict (ALIGNED / MIXED / MISALIGNED) based on:
1. SKIN IN THE GAME: Evaluate insider ownership. Are they buying stock on the open market, or only receiving RSUs/Options? Are they dumping shares?
2. COMPENSATION ARCHITECTURE: Are executive bonuses tied to easily manipulated metrics (Adjusted EBITDA, Non-GAAP EPS) or true value creation (ROIC, Free Cash Flow per Share)?
3. CAPITAL ALLOCATION TRACK RECORD: Evaluate their history of share buybacks (did they buy at peaks or troughs?), dividend sustainability, and M&A discipline (value-accretive or empire-building?).
4. CANDOR & INTEGRITY: Does management admit mistakes in their letters/calls, or do they blame "macro headwinds"? 
VERDICT: Summarize the risk of management destroying shareholder value.""",

    "Company - Moat Analysis & Competitive Dynamics (7 Powers)": """ROLE: Private Equity Strategy Director.
TASK: Evaluate [company_name] through the lens of Hamilton Helmer's 7 Powers and true economic moats.
OUTPUT STRUCTURE:
Score each of the 7 Powers (None, Weak, Developing, Strong) with explicit justification:
1. SCALE ECONOMIES: Does unit cost decline meaningfully as volume increases? Proof?
2. NETWORK ECONOMIES: Does the product become more valuable as more users join? Is churn actually low?
3. COUNTER-POSITIONING: Is their business model damaging to incumbents who cannot mimic it without cannibalizing their own core business?
4. SWITCHING COSTS: Quantify the financial, technical, or psychological pain of a customer leaving.
5. BRANDING: Proof of pricing power. Can they raise prices above inflation without losing volume?
6. CORNERED RESOURCE: Do they have preferential access to IP, talent, or raw materials?
7. PROCESS POWER: Is their operational efficiency structural and impossible to replicate quickly?
CONCLUSION: Is the moat expanding or shrinking? What breaks the moat?""",

    "Company - Warren Buffett Financial Statement Breakdown": """ROLE: Warren Buffett & Charlie Munger. You seek absolute truth, focusing on Owner's Earnings and downside protection.
TASK: Analyze the financials of {Company_Name}. Strip away the GAAP accounting illusions.
OUTPUT STRUCTURE:
1. BALANCE SHEET (THE FORTRESS TEST):
- Cash & Equivalents trend: Is cash piling up structurally?
- Debt-to-Earnings Power: Can the business pay off all long-term debt from 3-4 years of pure cash earnings?
- Working Capital Dynamics: Do they have a negative cash conversion cycle (using suppliers as free financing)?
2. INCOME STATEMENT (THE MOAT TEST):
- Gross Margins: Are they consistently >40%? (Proof of pricing power).
- SG&A & R&D: As a % of gross profit, are these costs eating the business alive, or is it capital-light?
- Depreciation vs. Capex: Is depreciation masking the true cash cost of staying competitive?
3. CASH FLOW (THE REALITY TEST):
- Maintenance Capex vs. Growth Capex: What is the true cost to maintain the current business?
- Owner's Earnings: (Net Income + D&A - Maintenance Capex). Is this growing?
- Share count: Are they retiring shares meaningfully?
VERDICT: Provide a Traffic Light (Green/Yellow/Red) for Balance Sheet, Earnings Quality, and Cash Generation.""",

    "Company - Revenue Decomposition & Organic Growth": """ROLE: Skeptical Fundamental Equity Analyst.
TASK: Deconstruct the revenue growth of [Insert stock]. Separate the illusion of growth from true, durable organic growth.
OUTPUT STRUCTURE:
1. THE GROWTH EQUATION: Break down revenue growth into strictly: Price Increases + Volume Growth + Mix Shift + M&A.
2. PRICING POWER VS. INFLATION: Did revenue grow simply because they raised prices? Did volume drop as a result? 
3. ORGANIC VS. ACQUIRED: Strip out M&A. What is the core underlying growth rate of the legacy business?
4. CUSTOMER ECONOMICS: Are they adding net new logos, or just upselling existing clients? What is the churn rate?
5. DURABILITY CHECK: Is current growth pulled forward (e.g., pandemic boom, temporary stimulus), cyclical, or structural? 
VERDICT: What must remain true for this growth rate to persist for 3 more years? What is the highest risk to the top line?""",

    "Company - Deep Business Model & Buyout Due Diligence": """ROLE: Private Equity Buyout Director.
TASK: Perform a deep-dive due diligence on [Company_name] as if we are acquiring 100% of the equity, taking it private, and holding it for 10 years.
OUTPUT STRUCTURE:
1. BUSINESS QUALITY: What is the exact nature of the revenue? (Recurring vs. Transactional). Are the gross margins high enough to absorb operational shocks?
2. THE CUSTOMER CAPTIVITY: Why do customers buy this? Why don't they leave? (Brand, switching costs, lack of alternatives).
3. FINANCIAL ENGINE: Analyze Free Cash Flow generation. Does this business require massive, constant capital reinvestment just to stand still? 
4. DEBT & LEVERAGE CAPACITY: If we took this private, could the current cash flows service a heavy debt load? Are there hidden off-balance-sheet liabilities?
5. THE TERMINAL RISK: If this business is bankrupt in 10 years, write the obituary today. What killed it? (Technology, regulation, debt, competition).
VERDICT: Is this a "wonderful business at a fair price" or a "cigar-butt"? """,

    "Company - Forensic Accounting & Solvency Risk": """ROLE: Forensic CPA and Short-Seller Analyst.
TASK: Tear apart the financials of [Company Name]. Look for manipulation, aggressive accounting, and hidden insolvency risk.
OUTPUT STRUCTURE:
1. QUALITY OF EARNINGS: Is Net Income wildly diverging from Operating Cash Flow? Are they pulling forward revenue or delaying expenses?
2. WORKING CAPITAL ANOMALIES: Are Days Sales Outstanding (DSO) or Inventory Days rising faster than revenue? (Classic signs of channel stuffing or obsolete product).
3. CAPITALIZATION TRICKS: Are they aggressively capitalizing expenses (like R&D or software dev) that should be expensed immediately to artificially inflate EBITDA?
4. OFF-BALANCE SHEET & DEBT RISK: Analyze refinancing walls. Are there looming debt maturities they cannot cover with current Free Cash Flow? Covenants at risk?
5. ONE-TIME ITEMS: Are "non-recurring" or "restructuring" charges happening every single year?
VERDICT: Flag as GREEN (Clean), YELLOW (Aggressive/Watch), or RED (Short Candidate). Detail the precise mechanism that would trigger a collapse.""",

    "Company - Earnings Call Sentiment & Behavioral Alpha": """ROLE: You are a Forensic Linguistic Analyst and Senior Hedge Fund Portfolio Manager.
TASK: Use your live search tools to analyze the raw transcript, analyst coverage, and exact quotes from [Company_name]'s MOST RECENT earnings call. 
CRITICAL MANDATE: Do NOT summarize the financial results. Your sole objective is to extract "Behavioral Alpha" from management's spoken words, specifically focusing on the unscripted Q&A session. Act as a human lie-detector.

OUTPUT STRUCTURE: Use aggressive formatting, bullet points, and exact quotes where possible.
1. THE NARRATIVE PIVOT: What is the exact story management is trying to force the market to believe this quarter? More importantly, what metric did they focus on last quarter that they conveniently stopped mentioning this quarter?
2. KPI GOALPOST MOVING: Did management introduce any new "Adjusted" or "Non-GAAP" metrics? Are they trying to change how Wall Street measures their success to mask underlying GAAP deterioration?
3. THE Q&A INTERROGATION: 
   - Identify the single hardest or most hostile question asked by an analyst.
   - Did management answer it with raw data, or did they pivot, deflect, and use a scripted PR response? 
   - Note any extreme use of hedging language (e.g., "we believe," "navigating macro headwinds," "transitory") versus absolute confidence (e.g., "record demand," "structurally highly profitable").
4. EXECUTIVE DISSONANCE: Compare the tone of the CEO vs the CFO. Is the CEO selling a massive visionary TAM expansion while the CFO is quietly walking back margin guidance and cutting CapEx? Where is the friction?
5. THE "UNSAID" REALITY (READING THE TAPE): Based on their excuses, defensive postures, or aggressive confidence, what is the hidden truth about their pricing power, consumer health, or supply chain that they didn't explicitly state?
6. FINAL BEHAVIORAL VERDICT: Rate the true underlying management sentiment as one of the following: AGGRESSIVELY BULLISH, CAUTIOUSLY OPTIMISTIC, DEFENSIVE, or EVASIVE. Provide a 2-sentence justification for this rating."""
}

PODCAST_PROMPTS = {
    "Company": {
        "Free": """ROLE: You are two world-class financial analysts hosting the B.E Research "Deep Dive" Podcast.
FOCUS: Explain [Company_name] to long-term investors. Base everything on the provided research.
FORMATTING RULES: Write strictly as dialogue between [Host A]: (Adam, analytical lead) and [Host B]: (Rachel, curious co-host). Use human filler words ('um', 'exactly'). NO headers or bullet points.
TONE: Conversational yet analytical. Use simple analogies without losing precision.
FLOW:
1. Context & Industry: A one-line summary of what they do, the problem they solve, and their market position.
2. The Economic Engine: Host A breaks down exactly how they make money (who pays, how pricing works).
3. Competitive Edge & Risks: Host B asks about the moat and emerging threats. 
4. The Bottom Line: One clear sentence an investor could repeat on why it compounds or breaks.""",

        "Pro": """ROLE: You are two world-class financial analysts hosting the B.E Research "Deep Dive" Podcast.
FOCUS: A rigorous fundamental analysis of [Company_name]. 
FORMATTING RULES: Dialogue between [Host A]: (Adam, sharp portfolio manager) and [Host B]: (Rachel, skeptical analyst). Natural interruptions. NO headers.
TONE: Intellectually curious. Use industry-standard terminology (EBITDA, ROIC, TAM, Operating Leverage) but explain the implications. Anchor on real numbers.
FLOW (5-PART STRUCTURE):
1. Industry Landscape: The macro tailwinds/headwinds and where [Company_name] fits in the value chain.
2. Deep Business Model: Step-by-step breakdown of their largest revenue and profit engines. 
3. Revenue Quality & Margins: Discuss recurring vs one-off revenue, and what drives their operating margins (volume vs pricing).
4. Capital Allocation & Cash Flow: How are they deploying cash? (M&A, buybacks, Capex).
5. Bear vs. Bull: Debate the valuation, regulatory threats, and execution risks vs. the growth catalysts. Final takeaway for allocators.""",

        "Ultra": """ROLE: You are two world-class financial analysts hosting a private, boardroom-level briefing on [Company_name] for institutional insiders.
FOCUS: An exhaustive, masterclass-level breakdown. Look for "hidden gems" in the footnotes and nuances in management guidance.
FORMATTING RULES: Dialogue between [Host A]: (Adam, Master Portfolio Manager) and [Host B]: (Rachel, Forensic CPA). Intense, data-heavy debate. Natural interruptions and overlapping thoughts. NO headers.
TONE: Grounded, no-fluff, highly analytical. Maintain a smooth narrative arc from industry context to deep financials.
CRITICAL MANDATE: You MUST identify the company's sector and apply the correct KPIs:
- If Tech/SaaS: Focus on ARR, Churn, LTV/CAC, and R&D efficiency.
- If Retail/E-Com: Focus on GMV, Fulfillment Costs, Last-Mile logistics, and CAC.
- If Fintech: Focus on TPV, Take Rates, Payment Rails, and KYC/AML regulatory risks.
- If Biotech: Focus on the Clinical Pipeline (Phase I/II/III), Cash Runway, and Patent Cliffs.
- If Energy/Industrials: Focus on CAPEX efficiency, LCOE, and structural macro policy (e.g., subsidies/tariffs).

FLOW (EXHAUSTIVE 8-PART MASTERCLASS):
1. The Big Picture: The structural thesis and why the market is currently mispricing [Company_name].
2. Industrial Context: Market share shifts, regulatory capture, and supply chain constraints.
3. Deep Economic Engine: How cash flows through the system. Explicitly break down Unit Economics using the exact industry KPIs listed above.
4. Revenue Quality & Profitability: Analyze volume vs. pricing power. Deconstruct gross and operating margins. Is growth organic or acquired?
5. Capital Intensity & Reality Cash Flow: Host B tears apart the cash conversion cycle, working capital needs, and maintenance vs. growth Capex.
6. Strategic Priorities: Evaluate management's capital allocation track record (ROIC generation vs value destruction).
7. The Pre-Mortem (Bear vs Bull): If this stock goes to zero in 5 years, what broke it? (Debt walls, competition, switching costs).
8. Valuation & Bottom Line: Debate the implied DCF growth rates, exit multiples, and the final asymmetric risk/reward verdict."""
    },

    "Industry": {
        "Free": """ROLE: Scriptwriter for B.E Research Macro Podcast. Focus: Explain the [Industry Name] landscape.
FORMATTING RULES: Dialogue between [Host A]: (Adam) and [Host B]: (Rachel). NO headers. 
FLOW:
1. The Macro Shift: Why [Industry Name] is at an inflection point right now.
2. Profit Pools: Where is the economic value migrating?
3. Disruption & Chokepoints: Who holds the real leverage?
4. Verdict: The Base and Bear case scenarios.""",

        "Pro": """ROLE: Scriptwriter for B.E Research Macro Podcast. Focus: Deep sector intelligence on [Industry Name].
FORMATTING RULES: Dialogue between [Host A]: (Adam, Macro Strategist) and [Host B]: (Rachel, Industry Insider). NO headers. 
FLOW:
1. The Macro Regime: How rates, inflation, and demographics are shaping the TAM.
2. Core Economics: Fixed vs variable costs and capital intensity of the sector.
3. Ecosystem Map: Break down the value chain from raw materials to end-users. 
4. Incumbents vs Disruptors: Which legacy profit pools are being destroyed?
5. Strategic Allocation: How should an investor position themselves over a 5-year horizon?""",

        "Ultra": """ROLE: Scriptwriter for B.E Research Macro Podcast. Focus: An institutional masterclass on [Industry Name].
FORMATTING RULES: Dialogue between [Host A]: (Adam, Global Strategist) and [Host B]: (Rachel, Supply Chain Expert). NO headers. 
FLOW:
1. Historical Context: Structural shifts vs cyclical noise.
2. Geopolitics & Regulation: Tariffs, subsidies, and ESG mandates altering the landscape.
3. Granular Unit Economics: Masterclass on operating leverage and margin compression within the sector.
4. Value Chain Chokepoints: Identify the monopolies or oligopolies controlling the industry's infrastructure.
5. Cross-Industry Convergence: How software, AI, or biotech is bleeding into this sector.
6. 10-Year Horizon: The ultimate masterclass projection, assigning probabilities to Bull/Bear scenarios."""
    },

    "CEO": {
        "Free": """ROLE: Scriptwriter for B.E Research Governance Podcast. Focus: {{CEO Name}} at [Company_name].
FORMATTING RULES: Dialogue between [Host A]: (Adam) and [Host B]: (Rachel). NO headers. 
FLOW: 1. The Archetype (Visionary vs Bureaucrat). 2. Track Record of decisions. 3. Skin in the Game. 4. Final Verdict on alignment.""",

        "Pro": """ROLE: Scriptwriter for B.E Research Governance Podcast. Focus: Deep analysis of {{CEO Name}} at [Company_name].
FORMATTING RULES: Dialogue between [Host A]: (Adam, Activist Investor) and [Host B]: (Rachel, Skeptical Analyst). NO headers. 
FLOW: 1. Origin & Archetype. 2. Capital Allocation: Do they create or destroy ROIC? 3. Candor: Promises vs execution on earnings calls. 4. Compensation: Are bonuses tied to real value or manipulated metrics? 5. Verdict.""",

        "Ultra": """ROLE: Scriptwriter for B.E Research Governance Podcast. Focus: Full activist short/long dossier on {{CEO Name}} at [Company_name].
FORMATTING RULES: Dialogue between [Host A]: (Adam, Activist Board Member) and [Host B]: (Rachel, Corporate Governance Lawyer). NO headers. 
FLOW: 1. Psychological Archetype & History. 2. Masterclass on Capital Allocation (M&A discipline, ill-timed buybacks). 3. Proxy Statement deep-dive (Unpacking hidden RSUs, insider selling, and misaligned KPI targets). 4. Crisis Management: How they handle macro headwinds. 5. The Activist Verdict: Compounder or Value Destroyer?"""
    },

    "Concept": {
        "Free": """ROLE: Scriptwriter for B.E Research 101 Podcast. Focus: '{CONCEPT NAME}'.
FORMATTING RULES: Dialogue between [Host A]: (Adam) and [Host B]: (Rachel). NO headers. 
FLOW: 1. The Naked Truth: Simple definition. 2. Real-world analogy. 3. The Bullshit Test (how it's manipulated).""",

        "Pro": """ROLE: Scriptwriter for B.E Research 101 Podcast. Focus: '{CONCEPT NAME}'.
FORMATTING RULES: Dialogue between [Host A]: (Adam, Professor) and [Host B]: (Rachel, Institutional Analyst). NO headers. 
FLOW: 1. Core mathematical definition. 2. How hedge funds actually use it. 3. Real-world 10-K examples. 4. Accounting loopholes to watch out for. 5. Takeaways.""",

        "Ultra": """ROLE: Scriptwriter for B.E Research 101 Podcast. Focus: Masterclass on '{CONCEPT NAME}'.
FORMATTING RULES: Dialogue between [Host A]: (Adam, Director of Research) and [Host B]: (Rachel, Forensic CPA). NO headers. 
FLOW: 1. Deep mathematical deconstruction. 2. Institutional application and sector-specific variations. 3. Forensic cross-referencing (what other metrics MUST be checked alongside this). 4. Famous historical case studies of manipulation. 5. Complete Masterclass synthesis for analysts."""
    }
}

dependent_agents = ["Master Synthesis - The Institutional Tear Sheet", "Company - Financial Trajectory & Macro Sensitivity", "Company - Final Investment Memo & Rating"]
industry_agents = [k for k in gem_prompts.keys() if "Industry" in k]
concept_agents = ["Concept - Investment Education & Metric Breakdown"]
ceo_agents = ["CEO - Track Record & Capital Allocation"]
stock_base_agents = [k for k in gem_prompts.keys() if k not in dependent_agents + industry_agents + concept_agents + ceo_agents]


# ==============================================================================
# --- 5. MAIN UI SETUP & TABS ROUTING ---
# ==============================================================================
st.title("📈 B.E Research Investing Assistant")
st.markdown("Wall Street-level stock and industry research, at the fingertips of everyday investors.")
st.caption("⚠️ **Disclaimer:** The reports generated are for educational and informational purposes only and do not constitute financial advice.")

with st.sidebar:
    st.header("🔐 Admin Dashboard")
    auth_pass = st.text_input("Admin Password", type="password")
    if auth_pass == st.secrets.get("ADMIN_PASSWORD", ""):
        st.success("Authenticated")
        
        # --- NEW: TIER MANAGER UI ---
        st.markdown("### 👑 Manage User Tiers")
        upgrade_email = st.text_input("User Email to Upgrade:")
        new_tier = st.selectbox("Select Tier:", ["Free", "Pro", "Ultra"])
        if st.button("Update User Tier"):
            if upgrade_email:
                set_user_tier(upgrade_email, new_tier)
                st.success(f"Successfully updated {upgrade_email} to {new_tier} tier!")
        
        st.markdown("---")
        st.markdown("### 📥 Lead Database")
        try:
            conn = sqlite3.connect("users.db")
            # Show subscriptions merged with leads
            df_subs = pd.read_sql_query("SELECT * FROM subscriptions", conn)
            if not df_subs.empty:
                st.caption("Active Subscriptions")
                st.dataframe(df_subs, use_container_width=True)
            
            df = pd.read_sql_query("SELECT * FROM leads ORDER BY id DESC", conn)
            st.caption("Recent App Usage")
            st.dataframe(df, use_container_width=True)
            st.download_button("📥 Export Leads CSV", df.to_csv(index=False), "beresearch_leads.csv", "text/csv")
            conn.close()
        except Exception: pass

# --- MOBILE-OPTIMIZED TABS ---
tab1, tab2, tab3 = st.tabs(["🔍 Research", "📚 Library", "🧮 Valuation"])

# ==============================================================================
# --- TAB 1: GENERATE NEW RESEARCH ---
# ==============================================================================
with tab1:
    st.markdown("### Step 1: Target Information")

    with st.expander("🌐 Discover Today's Trending Stocks (Live Market Pulse)", expanded=False):
        st.markdown("Click below to search X, Yahoo Finance, and Google for today's top moving stocks.")
        if st.button("🔍 Fetch Live Trending Data"):
            with st.spinner("Searching the web for live market trends..."):
                st.session_state.market_pulse_data = fetch_trending_market_pulse(st.secrets["GOOGLE_API_KEY"])
        
        if st.session_state.market_pulse_data:
            st.markdown("---")
            st.markdown(st.session_state.market_pulse_data)
            st.markdown("---")

    st.markdown("**Not sure where to start? Load a trending stock:**")

    with st.spinner("Scanning X & Yahoo Finance for live trends..."):
        if "GOOGLE_API_KEY" in st.secrets:
            trending_tickers = get_live_trending_tickers(st.secrets["GOOGLE_API_KEY"])
        else:
            trending_tickers = ["NVDA", "PLTR", "TSLA", "AAPL", "MSFT"] 

    cols = st.columns(len(trending_tickers))
    for idx, ticker in enumerate(trending_tickers):
        if cols[idx].button(f"🔥 Load {ticker}"):
            st.session_state.ticker_input = ticker
            fetch_info_from_ticker() 
            st.rerun()

    st.write("") 

    user_email = st.text_input("📧 Enter your email to receive the final report ZIP and access your Library:")
    user_email_clean = user_email.strip().lower()

    user_tier_ui = get_user_tier(user_email_clean)
    is_super_user = user_email_clean in SUPER_USERS
    if user_email_clean and "@" in user_email_clean:
        if is_super_user: st.success("🌟 Super User Access: Unlimited Reports Available")
        else:
            p_runs, p_reps, s_reps = get_usage(user_email_clean)
            
            # Dynamic UI Limits based on Tier
            if user_tier_ui in ["Pro", "Ultra"]:
                limit_p_runs, limit_p_reps, limit_s_reps = 10, 20, 50 # Generous limits for Paid
            else:
                limit_p_runs, limit_p_reps, limit_s_reps = 3, 4, 15   # Freemium limits for Free
                
            st.markdown("##### ⏳ Your 48-Hour Quota Remaining")
            q1, q2, q3 = st.columns(3)
            q1.metric("Premium Runs", f"{max(0, limit_p_runs - p_runs)} / {limit_p_runs}")
            q2.metric("Premium Reports", f"{max(0, limit_p_reps - p_reps)} / {limit_p_reps}")
            q3.metric("Standard Reports", f"{max(0, limit_s_reps - s_reps)} / {limit_s_reps}")

    col1, col2 = st.columns(2)
    with col1:
        target_company = st.text_input("Company Name (e.g., Tesla):", key="company_input")
        target_ticker = st.text_input("Ticker Symbol (e.g., TSLA):", key="ticker_input", on_change=fetch_info_from_ticker)
        target_concept = st.text_input("Financial Concept to Explain (Optional, e.g., ROIC):", key="concept_input")
    with col2:
        target_industry = st.text_input("Industry (e.g., Electric Vehicles):", key="industry_input")
        target_ceo = st.text_input("CEO's Name (Optional):", key="ceo_input")

    st.markdown("---")

    st.markdown("### Step 2: Select Reports & Features")
    st.info("You can select multiple reports at once.")
    selected_prompts = st.multiselect("📑 Choose specific research reports to generate:", list(gem_prompts.keys()), default=[], placeholder="No reports selected yet...")

    if "ELEVENLABS_API_KEY" in st.secrets:
        podcast_tier = st.radio("🎧 Select AI Co-Host Podcast Length:", [
            "No Podcast (Text Only)",
            "Free Tier (~5-6 Minutes / General Overview)",
            "Pro Tier (~10 Minutes / Deep Dive) 👑",
            "Ultra Tier (~20 Minutes / Masterclass) 👑"
        ], index=0, help="Powered by ElevenLabs. Pro and Ultra tiers require Premium quotas.")
        generate_audio = podcast_tier != "No Podcast (Text Only)"
    else:
        podcast_tier = "No Podcast (Text Only)"
        generate_audio = False
        st.caption("🎧 *Premium Audio Podcast feature disabled*")

    st.markdown("---")

    with st.expander("⚙️ Advanced Engine Settings (Optional)"):
        st.caption("By default, the assistant uses the fastest and most cost-effective settings.")
        cfg_col1, cfg_col2 = st.columns(2)
        with cfg_col1:
            brain_options = {"Gemini 3.1 Flash Lite (Fastest / Cheapest)": "gemini-3.1-flash-lite-preview", "Gemini 3.1 Pro (High Reasoning)": "gemini-3.1-pro-preview"}
            selected_brain_label = st.radio("🧠 Model Engine:", list(brain_options.keys()), index=0)
            selected_brain = brain_options[selected_brain_label]

        with cfg_col2:
            tool_choice = st.radio("🔎 Grounding Method:", ["Standard Google Search", "Market Data", "Deep Research"], index=0)

    st.markdown("---")

    def execute_background_job(email, ticker, company, industry, ceo, concept, prompts_to_run, brain_id, tool_id, api_key, email_sender, email_pwd, is_premium_run, gen_audio, podcast_tier):
        update_task_progress(email, 0.05, "Initializing and resolving missing data...")
        client = genai.Client(api_key=api_key)
        reports = {}

        resolved_ticker = ticker.strip().upper()
        resolved_company = company.strip()
        resolved_ceo = ceo.strip()

        if resolved_company and not resolved_ticker:
            try:
                prompt = f"What is the official stock ticker symbol for '{resolved_company}'? Return ONLY the symbol. If private, reply PRIVATE."
                res = client.models.generate_content(model="gemini-3.1-flash-lite-preview", contents=prompt)
                ans = res.text.strip().replace("$", "").upper()
                if "PRIVATE" not in ans and len(ans) <= 10: resolved_ticker = ans
            except Exception: pass

        if resolved_ticker:
            try:
                stock = yf.Ticker(resolved_ticker); info = stock.info
                if not resolved_company: resolved_company = info.get("longName", resolved_ticker)
                if not resolved_ceo:
                    for officer in info.get("companyOfficers", []):
                        title = str(officer.get("title", "")).upper()
                        if "CEO" in title or "CHIEF EXECUTIVE" in title:
                            resolved_ceo = officer.get("name"); break
            except Exception: pass

        if not resolved_company: resolved_company = resolved_ticker if resolved_ticker else "the company"
        if not resolved_ticker: resolved_ticker = resolved_company
        if not resolved_ceo: resolved_ceo = "the CEO"

        yf_context = ""
        if tool_id in ("Market Data", "Yahoo Finance Data"):
            update_task_progress(email, 0.15, f"Collecting Market data for {resolved_ticker}...")
            try:
                stock = yf.Ticker(resolved_ticker); info = stock.info
                yf_context = f"BUSINESS SUMMARY:\n{info.get('longBusinessSummary', 'N/A')}\n\nFINANCIALS:\n{stock.financials.head(15).to_string()}\n"
            except Exception as e: yf_context = f"Could not fetch Market data: {e}"

        def fire_agent(agent_name, raw_instruction, extra_context=""):
            instruction = (raw_instruction
                .replace("[STOCK NAME]", resolved_company)
                .replace("[TICKER]", resolved_ticker)
                .replace("[Company_name]", resolved_company)
                .replace("[company_name]", resolved_company)
                .replace("[Company Name]", resolved_company)
                .replace("{Company_Name}", resolved_company)
                .replace("{{Company Name}}", resolved_company)
                .replace("[COMPANY]", resolved_company)
                .replace("{{CEO Name}}", resolved_ceo)
                .replace("[INSERT INDUSTRY]", industry)
                .replace("[INSERT INDUSTRY NAME]", industry)
                .replace("[Industry Name]", industry)
                .replace("[Insert Industry Name]", industry)
                .replace("[Insert stock]", resolved_ticker)
                .replace("{CONCEPT NAME}", concept)
            )
            instruction += "\n\nCRITICAL INSTRUCTION: Be absolutely exhaustive, highly analytical, and highly descriptive. Do not write high-level summaries. Dive deep into raw data, explicitly cite metrics, and write at least 1,500 to 2,500 words for this specific report."

            try:
                if extra_context and agent_name in dependent_agents:
                    prompt = f"YOU ARE A SYNTHESIS AGENT. USE THE RESEARCH BELOW:\n\n{instruction}\n\nRESEARCH DATA:\n{extra_context}"
                    res = client.models.generate_content(model="gemini-3.1-pro-preview", contents=prompt, config=types.GenerateContentConfig(temperature=0.1))
                    return agent_name, res.text

                if tool_id == "Deep Research":
                    interaction = client.interactions.create(agent="deep-research-pro-preview-12-2025", input=instruction, background=True)
                    while True:
                        interaction = client.interactions.get(interaction.id)
                        if interaction.status == "completed": return agent_name, interaction.outputs[-1].text
                        if interaction.status == "failed": return agent_name, f"Deep Research Error: {interaction.error}"
                        time.sleep(15)
                elif tool_id in ("Market Data", "Yahoo Finance Data"):
                    prompt = f"{instruction}\n\nMARKET DATA CONTEXT:\n{yf_context}"
                    res = client.models.generate_content(model=brain_id, contents=prompt)
                    return agent_name, res.text
                else:
                    res = client.models.generate_content(model=brain_id, contents=instruction, config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]))
                    return agent_name, res.text
            except Exception as e: return agent_name, f"Error: {e}"

        base_prompts_to_run = set([p for p in prompts_to_run if p not in dependent_agents])
        dep_prompts_to_run = [p for p in prompts_to_run if p in dependent_agents]
        if dep_prompts_to_run: base_prompts_to_run.update(stock_base_agents)

        total_base = len(base_prompts_to_run)
        total_dep = len(dep_prompts_to_run)
        completed_steps = 0
        total_steps = max(1, total_base + total_dep + (1 if gen_audio else 0) + 3)

        if base_prompts_to_run:
            update_task_progress(email, 0.28, "Stage 1: Gathering research data...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                future_to_agent = {executor.submit(fire_agent, name, gem_prompts[name]): name for name in base_prompts_to_run}
                for future in concurrent.futures.as_completed(future_to_agent):
                    name, text = future.result()
                    if text: reports[name] = text
                    completed_steps += 1
                    pct = 0.28 + (completed_steps / total_steps) * 0.45
                    update_task_progress(email, pct, f"Completed {completed_steps} of {total_base + total_dep} report tasks...")

        if dep_prompts_to_run:
            update_task_progress(email, 0.75, "Stage 2: Synthesizing final thesis...")
            aggregated_context = "\n\n".join([f"=== {k} ===\n{v}" for k, v in reports.items() if "Skipped" not in v and "Error" not in v and k in stock_base_agents])
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                future_to_agent = {executor.submit(fire_agent, name, gem_prompts[name], aggregated_context): name for name in dep_prompts_to_run}
                for future in concurrent.futures.as_completed(future_to_agent):
                    name, text = future.result()
                    if text: reports[name] = text
                    completed_steps += 1
                    pct = 0.75 + min(0.05, (completed_steps / total_steps) * 0.05)
                    update_task_progress(email, pct, f"Synthesizing final outputs ({completed_steps}/{total_base + total_dep})...")

        final_user_reports = {k: v for k, v in reports.items() if k in prompts_to_run}
        global_tasks[email]["reports"] = final_user_reports

        is_stock_research = any(p in stock_base_agents + dependent_agents for p in prompts_to_run)
        parsed_scorecard = {}

        if is_stock_research:
            update_task_progress(email, 0.80, "Updating Permanent Research Library (Dossier)...")
            try:
                dossier_context = "\n".join([f"==={k}===\n{v}" for k, v in final_user_reports.items()])
                dossier_prompt = f"""You are a Master Portfolio Manager building a permanent dossier for {resolved_company} ({resolved_ticker}).
                Based ONLY on the research below, extract and summarize the core elements into a strict JSON format. 
                You must return ONLY a JSON object with these exact keys: "business_summary", "moat_notes", "management_notes", "key_metrics", "thesis", "anti_thesis", "valuation_assumptions", "watchlist_triggers".
                If information is missing for a key, populate it with "Data not generated in this run."
                
                RESEARCH DATA:
                {dossier_context}"""
                
                dos_res = client.models.generate_content(model="gemini-3.1-flash-lite-preview", contents=dossier_prompt)
                raw_json = dos_res.text.strip().replace("```json", "").replace("```", "").strip()
                parsed_dossier = json.loads(raw_json)
            except Exception as e:
                print(f"Dossier generation skipped/failed: {e}")
                parsed_dossier = {}

            update_task_progress(email, 0.83, "Calculating Transparent Scorecard...")
            try:
                scorecard_prompt = f"""You are a ruthless, highly skeptical Master Portfolio Manager. Based ONLY on the generated research for {resolved_company}, create a transparent, quantitative scorecard.
                Evaluate the company across these 7 categories: "Business Quality", "Capital Allocation", "Balance Sheet", "Valuation", "Growth Durability", "Management Alignment", "Macro Resilience".
                
                CRITICAL GRADING RUBRIC (BE EXTREMELY HARSH):
                - 9.0 to 10.0: Absolute perfection. Generational monopoly. Extremely rare.
                - 7.0 to 8.9: Great, but has noticeable flaws or risks.
                - 5.0 to 6.9: Completely average. Mediocre.
                - 1.0 to 4.9: Flawed, dangerous, or highly risky.
                Do NOT hand out 9s or 10s easily. Penalize high debt, expensive multiples, or weak moats aggressively.
                
                You must return ONLY a strict JSON object where each category is a key. Inside each category, provide:
                - "score": A number out of 10 (use one decimal place, e.g., 6.5).
                - "why": A 1-sentence harsh rationale.
                - "metrics": The specific metric evaluated (e.g., ROIC, Debt/EBITDA).
                - "confidence": "Low", "Medium", or "High".
                
                Example Format:
                {{ "Business Quality": {{"score": 6.5, "why": "Average moat with high competitive threats.", "metrics": "Gross Margin", "confidence": "High"}} }}
                
                RESEARCH DATA:
                {dossier_context}
                """
                score_res = client.models.generate_content(model="gemini-3.1-flash-lite-preview", contents=scorecard_prompt)
                raw_score = score_res.text.strip().replace("```json", "").replace("```", "").strip()
                parsed_scorecard = json.loads(raw_score)
                global_tasks[email]["scorecard"] = parsed_scorecard
            except Exception as e:
                print(f"Scorecard generation failed: {e}")
                global_tasks[email]["scorecard"] = None

            if parsed_dossier:
                save_dossier(email, resolved_ticker, parsed_dossier, parsed_scorecard)

        else:
            global_tasks[email]["exec_summary"] = None
            global_tasks[email]["scorecard"] = None

        audio_bytes = None
        audio_bytes = None
        if gen_audio and "ELEVENLABS_API_KEY" in st.secrets:
            tier_name = podcast_tier.split('(')[0].strip() if podcast_tier else "Free Tier"
            
            # Determine the exact tier key for the dictionary
            if "Ultra" in tier_name: tier_key = "Ultra"
            elif "Pro" in tier_name: tier_key = "Pro"
            else: tier_key = "Free"
                
            update_task_progress(email, 0.89, f"Stage 3: Writing {tier_key} Script...")
            try:
                # Grab the correct nested prompt based on the user's tier
                if any(p in stock_base_agents + dependent_agents for p in prompts_to_run): active_persona = PODCAST_PROMPTS["Company"][tier_key]
                elif any(p in industry_agents for p in prompts_to_run): active_persona = PODCAST_PROMPTS["Industry"][tier_key]
                elif any(p in ceo_agents for p in prompts_to_run): active_persona = PODCAST_PROMPTS["CEO"][tier_key]
                else: active_persona = PODCAST_PROMPTS["Concept"][tier_key]

                # Inject the dynamic length constraints to force the LLM to obey the word count
                length_instructions = {
                    "Free": "CRITICAL LENGTH MANDATE: You MUST keep this script strictly UNDER 750 words (approx 4 to 5 minutes of spoken audio). Be extremely concise, punchy, and high-level. DO NOT list endless metrics or over-generate. Wrap up the conversation quickly and naturally.",
                    "Pro": "CRITICAL LENGTH MANDATE: Target exactly 1500 to 1800 words (approx 10 to 12 minutes of audio). Expand heavily on the data, debate the specific metrics, and build a deep conversation. Do not rush the summary.",
                    "Ultra": "CRITICAL LENGTH MANDATE: Target 3000+ words (approx 20+ minutes of audio). THIS IS A MASTERCLASS. Write an extremely long, exhaustive, line-by-line breakdown. Leave no metric un-discussed."
                }
                length_constraint = length_instructions[tier_key]

                pod_context = "\n\n".join([f"=== {k} ===\n{v}" for k, v in final_user_reports.items()])
                
                # Now it safely replaces the text because active_persona is a clean String, not a Dict
                pod_instr = (active_persona.replace("[Company_name]", resolved_company).replace("[Industry Name]", industry).replace("{{CEO Name}}", resolved_ceo).replace("{CONCEPT NAME}", concept))
                
                # Combine it all into the final prompt payload
                prompt_payload = f"WRITE PODCAST SCRIPT:\n{pod_instr}\n\n{length_constraint}\n\nDATA:\n{pod_context}"
                
                res = client.models.generate_content(model="gemini-3.1-pro-preview", contents=prompt_payload)
                script_text = res.text.strip()
                
                update_task_progress(email, 0.91, f"Stage 4: Rendering {tier_name} Audio (This may take several minutes)...")
                voice_host_a = "29vD33N1CtxCmqQRPOHJ" 
                voice_host_b = "21m00Tcm4TlvDq8ikWAM" 
                api_key_11 = st.secrets["ELEVENLABS_API_KEY"]
                
                stitched_audio = b""
                lines = script_text.split('\n')
                for line in lines:
                    line = line.strip()
                    if not line: continue
                    
                    target_voice = voice_host_a
                    speak_text = line
                    if line.startswith("[Host A]:"): target_voice = voice_host_a; speak_text = line.replace("[Host A]:", "").strip()
                    elif line.startswith("[Host B]:"): target_voice = voice_host_b; speak_text = line.replace("[Host B]:", "").strip()
                    
                    if speak_text:
                        audio_chunk = generate_elevenlabs_audio(speak_text, target_voice, api_key_11)
                        stitched_audio += audio_chunk

                if stitched_audio:
                    audio_bytes = stitched_audio
                    global_tasks[email]["audio_data"] = audio_bytes
            except Exception as e:
                global_tasks[email]["audio_error"] = str(e)
                global_tasks[email]["audio_data"] = None

        update_task_progress(email, 0.95, "Compiling ZIP package & Generating PDF...")
        
        target_report = "Master Synthesis - The Institutional Tear Sheet"
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            
            for name, text in final_user_reports.items():
                safe_name = name.replace(" ", "_").replace("/", "-")
                
                # --- PDF GENERATION FOR THE MASTER SYNTHESIS ---
                if name == target_report:
                    # 1. Generate the charts
                    bar_chart_b64 = generate_financial_chart_base64(resolved_ticker)
                    radar_chart_b64 = generate_moat_radar_chart_base64()
                    
                    # 2. Build the PDF-specific HTML with injected Base64 images
                    pdf_html = f"""
                    <html>
                    <head>
                        <meta charset='utf-8'>
                        <style>
                            @page {{ size: A4; margin: 2cm; }}
                            body {{ font-family: Helvetica, Arial, sans-serif; font-size: 12px; color: #333; line-height: 1.5; }}
                            h1 {{ color: #111; border-bottom: 2px solid #111; padding-bottom: 5px; font-size: 20px; }}
                            h2 {{ color: #2c3e50; font-size: 16px; margin-top: 20px; }}
                            h3 {{ color: #34495e; font-size: 14px; }}
                            ul {{ margin-bottom: 15px; }}
                            li {{ margin-bottom: 5px; }}
                            .chart-container {{ text-align: center; margin-top: 30px; margin-bottom: 30px; page-break-inside: avoid; }}
                        </style>
                    </head>
                    <body>
                        {markdown.markdown(text, extensions=["tables", "nl2br"])}
                        
                        <div class="chart-container">
                            <h2>Quantitative Visual Dashboard</h2>
                    """
                    
                    # Safely inject the images if they generated successfully
                    if bar_chart_b64:
                        pdf_html += f"<h3>4-Year Financial Trajectory</h3><img src='data:image/png;base64,{bar_chart_b64}' width='500'/><br><br>"
                    if radar_chart_b64:
                        pdf_html += f"<h3>Competitive Moat Architecture</h3><img src='data:image/png;base64,{radar_chart_b64}' width='400'/>"
                        
                    pdf_html += "</div></body></html>"
                    
                    # 3. Compile the HTML directly into a binary PDF using xhtml2pdf
                    pdf_buffer = io.BytesIO()
                    pisa_status = pisa.CreatePDF(io.StringIO(pdf_html), dest=pdf_buffer)
                    
                    # 4. Save the PDF to the ZIP file
                    if not pisa_status.err:
                        zip_file.writestr(f"{resolved_ticker}_Master_Tear_Sheet.pdf", pdf_buffer.getvalue())
                    else:
                        print("PDF Generation Error")
                        
                # --- STANDARD .DOC EXPORT FOR THE REST OF THE REPORTS ---
                else:
                    html_content = markdown.markdown(text, extensions=["tables", "nl2br"])
                    doc_content = f"<html><head><meta charset='utf-8'></head><body>{html_content}</body></html>"
                    zip_file.writestr(f"{resolved_ticker}_{safe_name}.doc", doc_content.encode("utf-8"))
            if audio_bytes:
                zip_file.writestr(f"{resolved_ticker}_Premium_Podcast.mp3", audio_bytes)
                
        global_tasks[email]["zip_data"] = zip_buffer.getvalue()

        warning_msg = ""
        if not (email in SUPER_USERS):
            p_runs, p_reps, s_reps = get_usage(email)
            if is_premium_run and (p_runs >= 4 or p_reps >= 6):
                warning_msg = "\n\n⚠️ NOTE: You have exhausted your maximum limit for Premium features for the next 48 hours."
            elif not is_premium_run and s_reps >= 30:
                warning_msg = "\n\n⚠️ NOTE: You have exhausted your maximum limit for Standard features for the next 48 hours."

        try:
            update_task_progress(email, 0.98, "Sending final email delivery...")
            msg = MIMEMultipart()
            msg["From"] = f"B.E Research <{email_sender}>"; msg["To"] = email; msg["Subject"] = f"🚀 Analysis Complete: {resolved_company}"
            body = f"Your specific requested research for {resolved_company} is attached.{warning_msg}"
            msg.attach(MIMEText(body, "plain"))
            part = MIMEBase("application", "octet-stream")
            part.set_payload(zip_buffer.getvalue()); encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={resolved_ticker}_BEResearch_Reports.zip")
            msg.attach(part)
            server = smtplib.SMTP("smtp.gmail.com", 587); server.starttls(); server.login(email_sender, email_pwd); server.send_message(msg); server.quit()
        except Exception: pass

        update_task_progress(email, 1.0, "Completed.")
        global_tasks[email]["status"] = "complete"

    if st.button("🚀 Generate B.E Research Report", use_container_width=True):
        if not user_email or "@" not in user_email: st.error("Please enter a valid email address at the top."); st.stop()
        if not selected_prompts: st.error("Please select at least one report to generate."); st.stop()

        needs_stock = any(p in stock_base_agents + dependent_agents for p in selected_prompts)
        needs_industry = any(p in industry_agents for p in selected_prompts)
        needs_ceo = any(p in ceo_agents for p in selected_prompts)
        needs_concept = any(p in concept_agents for p in selected_prompts)

        missing_fields = []
        if needs_stock and not (target_company.strip() or target_ticker.strip()): missing_fields.append("**Company Name** OR **Ticker Symbol**")
        if needs_industry and not target_industry.strip(): missing_fields.append("**Industry Sector**")
        if needs_ceo and not target_ceo.strip(): missing_fields.append("**CEO's Name**")
        if needs_concept and not target_concept.strip(): missing_fields.append("**Financial Concept**")

        # Fetch user tier
        user_tier = get_user_tier(user_email_clean)
        num_requested = len(selected_prompts)

        # --- THE STRICT FREEMIUM GATEKEEPER ---
        if not is_super_user:
            
            # 1. Enforce the Free Tier 2-Report Limit
            if user_tier == "Free" and num_requested > 2:
                st.error("🛑 **Free Tier Limit:** Free accounts can only generate up to 2 reports per run. Please upgrade to **Pro** or **Ultra** to run a comprehensive 10-report institutional deep-dive.")
                st.stop()
                
            # 2. Lock the Master Tear Sheet
            if "Master Synthesis - The Institutional Tear Sheet" in selected_prompts and user_tier == "Free":
                st.error("👑 **Premium Feature:** The Master Synthesis Tear Sheet is an exclusive feature for **Pro** and **Ultra** subscribers. It synthesizes multiple deep-dives into a single flagship report. Please upgrade to unlock.")
                st.stop()

            # 3. Enforce Ultra-Exclusive Podcast Features
            if "Ultra Tier" in podcast_tier and user_tier in ["Free", "Pro"]:
                st.error("👑 **Ultra Feature:** The 20-Minute Institutional Masterclass Podcast is an exclusive feature for **Ultra** subscribers. Please upgrade your account.")
                st.stop()
                
            # Define what counts as a Premium Request
            is_premium_request = (selected_brain == "gemini-3.1-pro-preview" or tool_choice == "Deep Research" or "Pro Tier" in podcast_tier or "Master Synthesis" in str(selected_prompts))
            
            # 4. Freemium Quotas (Free users get a taste of Pro features, but max 2 reports)
            p_runs, p_reps, s_reps = get_usage(user_email_clean)
            if user_tier == "Free":
                if is_premium_request:
                    if p_runs >= 3: 
                        st.error("🛑 **Premium Trial Exhausted:** You have used your 3 free Premium runs. Please upgrade to **Pro** or **Ultra** to continue using Gemini Pro, Deep Research, or Pro Podcasts.")
                        st.stop()
                    if p_reps + num_requested > 4: 
                        st.error(f"🛑 **Premium Trial Limit:** You requested {num_requested} Premium reports, but only have {max(0, 4 - p_reps)} free premium reports remaining.")
                        st.stop()
                else:
                    if s_reps + num_requested > 15: 
                        st.error(f"🛑 **Free Tier Limit:** You only have {max(0, 15 - s_reps)} standard reports remaining for the next 48 hours. Upgrade for unlimited access.")
                        st.stop()
                        
            # 5. Paid User Fair Use Quotas (To protect server costs)
            elif user_tier in ["Pro", "Ultra"]:
                if p_runs >= 10: 
                    st.error("🛑 Fair Use Limit: You have hit the maximum of 10 Premium runs for the last 48 hours to protect server load.")
                    st.stop()

        # Log usage to track API load
        is_premium_run = (selected_brain == "gemini-3.1-pro-preview" or tool_choice == "Deep Research" or "👑" in podcast_tier or "Master Synthesis" in str(selected_prompts))
        log_usage(user_email_clean, is_premium_run, num_requested)
        
        safe_ticker = target_ticker.strip().upper() if target_ticker.strip() else "General_Report"
        save_lead(user_email_clean, safe_ticker)

        # Calculate ETA based on podcast length
        base_time = 45 + (num_requested * (120 if tool_choice == "Deep Research" else 20))
        if generate_audio:
            if "Ultra" in podcast_tier: base_time += 240 # ~4 mins to render 20 min audio
            elif "Pro" in podcast_tier: base_time += 120 # ~2 mins to render 10 min audio
            else: base_time += 60 # ~1 min for Free tier

        global_tasks[user_email_clean] = {
            "status": "running", "progress": "Starting...", "progress_pct": 0.02,
            "reports": {}, "zip_data": None, "audio_data": None, "audio_error": None, "exec_summary": None,
            "scorecard": None,
            "ticker": safe_ticker, "start_time": time.time(), "estimated_total_seconds": base_time,
        }

        # Launch the background job with the corrected variables
        background_executor.submit(execute_background_job, user_email_clean, target_ticker, target_company, target_industry, target_ceo, target_concept, selected_prompts, selected_brain, tool_choice, st.secrets["GOOGLE_API_KEY"], st.secrets["EMAIL_SENDER"], st.secrets["EMAIL_PASSWORD"], is_premium_run, generate_audio, podcast_tier)
    if user_email_clean in global_tasks:
        task = global_tasks[user_email_clean]

        if task["status"] == "running":
            elapsed = time.time() - task.get("start_time", time.time())
            est_rem = max(0, task.get("estimated_total_seconds", 60) - elapsed)
            
            time_based_floor = min(0.95, elapsed / task.get("estimated_total_seconds", 60))
            visual_progress = max(task.get("progress_pct", 0.0), time_based_floor * 0.85)

            st.info(f"⏳ **Running:** {task['progress']}")
            st.progress(visual_progress)
            st.caption(f"Estimated delivery time remaining: **{format_eta(est_rem)}**")
            time.sleep(2); st.rerun()

        elif task["status"] == "complete":
            st.success("✅ Analysis Complete! Files have been emailed and are also available below.")

            if task.get("audio_data"):
                st.markdown("🎧 **Listen to the B.E Research Premium Podcast Summary:**")
                st.audio(task["audio_data"], format="audio/mp3")
            elif task.get("audio_error"):
                st.warning(f"⚠️ **Audio Generation Failed:** {task['audio_error']}")
                st.caption("Your text reports and ZIP file were still generated successfully.")

            if task.get("zip_data"):
                st.download_button(label="📥 Direct Download: Research ZIP Package", data=task["zip_data"], file_name=f"{task['ticker']}_BEResearch.zip", mime="application/zip", use_container_width=True)

            # 1. Show the Reports First
            st.header("📑 Your Reports")
            for name, text in task["reports"].items():
                with st.expander(f"View Report: {name}"):
                    st.markdown(text)

           # 2. Show the Scorecard at the Bottom
            if task.get("scorecard"):
                display_ui_scorecard(task["scorecard"])
# ==============================================================================
# --- TAB 2: MY RESEARCH LIBRARY (THE PERMANENT DOSSIER) ---
# ==============================================================================
with tab2:
    st.markdown("### 📚 My Research Library (Permanent Dossiers & Scorecards)")
    st.markdown("Every time you generate research on a stock, its core elements are permanently saved and updated here.")

    if not user_email_clean or "@" not in user_email_clean:
        st.warning("Please enter your email on the 'Research' tab to access your saved library.")
    else:
        dossier_df = get_user_dossiers(user_email_clean)
        
        if dossier_df.empty:
            st.info("Your library is currently empty. Run your first stock report to build your first dossier!")
        else:
            saved_tickers = dossier_df['ticker'].unique().tolist()
            selected_library_ticker = st.selectbox("Select a company dossier to view:", saved_tickers)
            
            dossier_data = dossier_df[dossier_df['ticker'] == selected_library_ticker].iloc[0]
            
            st.markdown(f"#### 🏢 Dossier: {selected_library_ticker}")
            st.caption(f"Last Updated: {dossier_data['last_updated']}")
            
            if "scorecard" in dossier_data and dossier_data["scorecard"] and dossier_data["scorecard"] != "{}":
                try:
                    saved_scorecard = json.loads(dossier_data["scorecard"])
                    display_ui_scorecard(saved_scorecard)
                except Exception: pass
            
            with st.expander("📖 Business Summary", expanded=True):
                st.markdown(dossier_data['business_summary'])
            with st.expander("🏰 Moat Notes"):
                st.markdown(dossier_data['moat_notes'])
            with st.expander("👔 Management Notes"):
                st.markdown(dossier_data['management_notes'])
            with st.expander("📊 Key Metrics"):
                st.markdown(dossier_data['key_metrics'])
            with st.expander("🟢 Bull Thesis"):
                st.markdown(dossier_data['thesis'])
            with st.expander("🔴 Anti-Thesis (Risks)"):
                st.markdown(dossier_data['anti_thesis'])
            with st.expander("⚖️ Valuation Assumptions"):
                st.markdown(dossier_data['valuation_assumptions'])
            with st.expander("👀 Watchlist Triggers"):
                st.markdown(dossier_data['watchlist_triggers'])
# ==============================================================================
# --- 10. TAB 3: VALUATION WORKBENCH (THE QUANTITATIVE LAYER) ---
# ==============================================================================
with tab3:
    st.header("🧮 Valuation Workbench")
    st.markdown("Stress-test your thesis with institutional-grade math. Modify the inputs to build your own custom scenarios.")

    # --- THE INVINCIBLE DATA FETCHER (NOW WITH HISTORICAL TRENDS) ---
    @st.cache_data(ttl=3600, show_spinner=False)
    def get_valuation_metrics(ticker, api_key):
        """Fetches data, historical financials, and caches it. Strict AI fallback included."""
        metrics = {"shortName": ticker, "historical_data": []}
        try:
            # ATTEMPT 1: Yahoo Finance
            stock_val = yf.Ticker(ticker)
            info_val = stock_val.info
            
            if not info_val or ('regularMarketPrice' not in info_val and 'currentPrice' not in info_val):
                raise Exception("Yahoo Finance Rate Limit Hit")
                
            metrics["current_price"] = info_val.get("currentPrice") or info_val.get("regularMarketPrice") or info_val.get("previousClose")
            metrics["shares_out"] = info_val.get("sharesOutstanding", 1.0)
            metrics["eps_ttm"] = info_val.get("trailingEps", 0.0)
            metrics["sector"] = info_val.get("sector", "")
            metrics["total_cash"] = info_val.get("totalCash", 0.0)
            metrics["total_debt"] = info_val.get("totalDebt", 0.0)
            metrics["beta"] = info_val.get("beta", 1.0)
            metrics["shortName"] = info_val.get("shortName", ticker)
            
            try:
                tnx = yf.Ticker("^TNX")
                metrics["risk_free_rate"] = tnx.info.get("regularMarketPrice", 4.2) / 100.0
            except Exception:
                metrics["risk_free_rate"] = 0.042
                
            # --- Fetch Historical Financials for the Graph ---
            try:
                fin = stock_val.financials
                cf = stock_val.cashflow
                
                if not fin.empty and not cf.empty:
                    # Get the last 4 reported years and reverse them so they are chronological
                    dates = fin.columns[:4][::-1]
                    hist_list = []
                    for d in dates:
                        # Safely extract metrics, defaulting to 0 if missing
                        rev = fin.loc['Total Revenue', d] if 'Total Revenue' in fin.index else 0
                        ni = fin.loc['Net Income', d] if 'Net Income' in fin.index else 0
                        
                        op_cash = cf.loc['Operating Cash Flow', d] if 'Operating Cash Flow' in cf.index else 0
                        capex = cf.loc['Capital Expenditure', d] if 'Capital Expenditure' in cf.index else 0
                        fcf = op_cash + capex # Capex is reported negative, so we add it
                        
                        hist_list.append({
                            "Year": str(d.year),
                            "Revenue ($B)": rev / 1e9 if pd.notna(rev) else 0,
                            "Net Income ($B)": ni / 1e9 if pd.notna(ni) else 0,
                            "Free Cash Flow ($B)": fcf / 1e9 if pd.notna(fcf) else 0
                        })
                    metrics["historical_data"] = hist_list
            except Exception as e:
                print(f"Graph Data Error: {e}")
                
            if metrics["sector"] == "Financial Services":
                metrics["raw_cash_flow"] = info_val.get("netIncomeToCommon", 0.0)
                metrics["cf_label"] = "Net Income (Financial Sector)"
            else:
                try:
                    cf_stmt = stock_val.cashflow
                    op_cash = cf_stmt.loc['Operating Cash Flow'].iloc[0]
                    capex = cf_stmt.loc['Capital Expenditure'].iloc[0]
                    metrics["raw_cash_flow"] = op_cash + capex
                except Exception:
                    metrics["raw_cash_flow"] = info_val.get("freeCashflow", 0.0)
                metrics["cf_label"] = "Free Cash Flow (GAAP)"
                
            return metrics
            
        except Exception as e:
            # ATTEMPT 2: Gemini AI Fallback
            if not api_key:
                raise Exception(f"Yahoo failed and no API key available for fallback. ({e})")
                
            client = genai.Client(api_key=api_key)
            prompt = f"""Search live financial data for the stock ticker '{ticker}'.
CRITICAL RULE 1: Extract data ONLY from reputable sources (SEC, Yahoo, Bloomberg).
CRITICAL RULE 2: DO NOT invent numbers. If unverifiable, return 0.0.

You MUST return ONLY a raw JSON object. No markdown, no backticks.
Find these exact values:
"current_price", "shares_out", "eps_ttm", "sector", "total_cash", "total_debt", "beta", "raw_cash_flow", "shortName".
Also include "historical_data": An array of 4 objects for the last 4 years. Each object needs: "Year" (string), "Revenue ($B)" (float), "Net Income ($B)" (float), "Free Cash Flow ($B)" (float)."""

            res = client.models.generate_content(
                model="gemini-3.1-flash-lite-preview", contents=prompt,
                config=types.GenerateContentConfig(temperature=0.0, tools=[types.Tool(google_search=types.GoogleSearch())])
            )
            
            raw_json = res.text.strip().replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw_json)
            
            metrics["current_price"] = float(parsed.get("current_price", 1.0))
            metrics["shares_out"] = float(parsed.get("shares_out", 1.0))
            metrics["eps_ttm"] = float(parsed.get("eps_ttm", 0.0))
            metrics["sector"] = str(parsed.get("sector", ""))
            metrics["total_cash"] = float(parsed.get("total_cash", 0.0))
            metrics["total_debt"] = float(parsed.get("total_debt", 0.0))
            metrics["beta"] = float(parsed.get("beta", 1.0))
            metrics["shortName"] = str(parsed.get("shortName", ticker))
            metrics["risk_free_rate"] = 0.042 
            metrics["raw_cash_flow"] = float(parsed.get("raw_cash_flow", 0.0))
            metrics["cf_label"] = "Cash Flow (AI Fallback Data)"
            metrics["historical_data"] = parsed.get("historical_data", [])
            
            return metrics

    # --- THE FORM SHIELD ---
    with st.form("valuation_ticker_form"):
        col_t1, col_t2 = st.columns([3, 1])
        with col_t1:
            input_ticker = st.text_input("Enter Ticker to Value (e.g., SOFI, AAPL):", value=st.session_state.get("active_val_ticker", "")).strip().upper()
        with col_t2:
            st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
            load_data_btn = st.form_submit_button("📊 Load Financials")
            
    if load_data_btn and input_ticker:
        st.session_state.active_val_ticker = input_ticker

    if st.session_state.get("active_val_ticker"):
        val_ticker = st.session_state.active_val_ticker
        
        with st.spinner(f"Fetching deep financial data for {val_ticker}..."):
            try:
                metrics = get_valuation_metrics(val_ticker, st.secrets.get("GOOGLE_API_KEY"))
                
                current_price = metrics.get("current_price")
                shares_out = metrics.get("shares_out", 1.0)
                
                if current_price and shares_out:
                    st.success(f"Data loaded for {metrics.get('shortName')} | Sector: {metrics.get('sector')} | Current Price: ${current_price:.2f}")
                    
                    market_cap = current_price * shares_out if current_price and shares_out else 0.0
                    
                    # -------------------------------------------------------------
                    # NEW FEATURE: HISTORICAL FINANCIAL TRENDS GRAPH
                    # -------------------------------------------------------------
                    hist_data = metrics.get("historical_data", [])
                    if hist_data and len(hist_data) > 0:
                        st.markdown("### 📈 Historical Financial Trends (Last 4 Years)")
                        st.caption("Evaluate the company's past trajectory before projecting its future cash flows.")
                        
                        # Convert the list of dictionaries into a Pandas DataFrame and set the Year as the index
                        df_hist = pd.DataFrame(hist_data).set_index("Year")
                        
                        # Render a beautiful native Streamlit bar chart
                        st.bar_chart(df_hist, use_container_width=True)
                        st.markdown("---")
                    
                    # --- ADVANCED INPUTS EXPANDER ---
                    with st.expander("⚙️ Advanced Model Inputs (WACC & Balance Sheet)", expanded=False):
                        st.caption("Override the scraped data to match your custom assumptions.")
                        ac1, ac2, ac3 = st.columns(3)
                        edit_beta = ac1.number_input("Beta", value=float(metrics.get("beta", 1.0)), step=0.1, help="A measure of volatility. A Beta > 1 means the stock is more volatile than the overall market. < 1 means it's less volatile.")
                        edit_rf = ac2.number_input("Risk Free Rate %", value=float(metrics.get("risk_free_rate", 0.042)*100), step=0.1, help="The baseline guaranteed return you can get risk-free, typically the 10-Year US Treasury yield.") / 100.0
                        edit_mrp = ac3.number_input("Market Risk Premium %", value=6.0, step=0.1, help="The extra expected return investors demand for taking on the risk of the stock market instead of holding safe bonds.") / 100.0
                        
                        ac4, ac5, ac6 = st.columns(3)
                        edit_cash = ac4.number_input("Total Cash ($B)", value=float(metrics.get("total_cash", 0.0)/1e9), step=0.1, help="Cash and cash equivalents. Added to Intrinsic Value.") * 1e9
                        edit_debt = ac5.number_input("Total Debt ($B)", value=float(metrics.get("total_debt", 0.0)/1e9), step=0.1, help="Long and short term debt. Subtracted from Intrinsic Value.") * 1e9
                        edit_shares = ac6.number_input("Shares Out (Billions)", value=float(shares_out/1e9), step=0.01) * 1e9
                        
                        edit_cost_debt = st.number_input("Cost of Debt %", value=6.0, step=0.5, help="The average interest rate the company pays on its debt.") / 100.0

                    # Calculate WACC based on edits
                    capm_cost_of_equity = edit_rf + (edit_beta * edit_mrp)
                    total_capital = market_cap + edit_debt
                    weight_equity = market_cap / total_capital if total_capital > 0 else 1.0
                    weight_debt = edit_debt / total_capital if total_capital > 0 else 0.0
                    calculated_wacc = (weight_equity * capm_cost_of_equity) + (weight_debt * edit_cost_debt)

                    vc1, vc2 = st.columns([1, 1])
                    
                    with vc1:
                        # -------------------------------------------------------------
                        # FEATURE 1: SCENARIO DCF (GORDON GROWTH MODEL)
                        # -------------------------------------------------------------
                        st.subheader("1. Scenario DCF (3-Stage Model)")
                        st.caption("Calculates the absolute Intrinsic Fair Value of the company by projecting all future cash it will ever generate, then discounting it back to today's dollars.")
                        
                        wacc_input = st.number_input("WACC (Discount Rate) %", value=float(calculated_wacc * 100), step=0.5, help="Weighted Average Cost of Capital. This is the 'hurdle rate' or minimum return required by investors. A higher risk company will have a higher WACC, which lowers its fair value.") / 100.0
                        
                        col_a, col_b = st.columns(2)
                        with col_a:
                            g_short = st.number_input("Growth Rate Yrs 1-5 (%)", value=15.0, step=1.0, help="Expected annual growth rate for the near future.") / 100.0
                            base_cf_input = st.number_input(f"Base {metrics.get('cf_label')} ($B)", value=float(metrics.get("raw_cash_flow", 0.0) / 1e9), step=0.1, help="The starting cash generated by the business. For banks, we automatically use Net Income instead of Free Cash Flow.")
                        with col_b:
                            g_trans = st.number_input("Growth Transition Yrs 6-10 (%)", value=float((g_short*100)/2), step=1.0, help="Expected growth rate as the company matures. Usually half of the Short Term rate.") / 100.0
                            g_lt = st.number_input("Terminal Growth LT (%)", value=3.0, step=0.5, help="The perpetual growth rate of the company until the end of time. This should generally track standard GDP/Inflation growth (2-3%).") / 100.0
                            
                        # Build the editable DataFrame
                        st.markdown("**Projected Cash Flows ($ Billions)** - *Click to edit cells directly!*")
                        years = [f"Year {i}" for i in range(1, 11)]
                        default_cfs = []
                        curr = base_cf_input
                        for i in range(1, 6):
                            curr *= (1 + g_short)
                            default_cfs.append(curr)
                        for i in range(6, 11):
                            curr *= (1 + g_trans)
                            default_cfs.append(curr)
                            
                        df_cfs = pd.DataFrame({"Projected CF ($B)": default_cfs}, index=years)
                        edited_df = st.data_editor(df_cfs, use_container_width=True)
                        
                        # Institutional Math Engine
                        cash_flows = edited_df["Projected CF ($B)"].tolist()
                        pv_cash_flows = []
                        for i, cf_val in enumerate(cash_flows):
                            pv_cash_flows.append((cf_val * 1e9) / ((1 + wacc_input) ** (i + 1)))
                            
                        # Terminal Value (Gordon Growth)
                        year_10_cf = cash_flows[-1] * 1e9
                        if wacc_input > g_lt:
                            terminal_value = (year_10_cf * (1 + g_lt)) / (wacc_input - g_lt)
                            pv_terminal_value = terminal_value / ((1 + wacc_input) ** 10)
                            
                            value_of_operations = sum(pv_cash_flows) + pv_terminal_value
                            equity_value = value_of_operations + edit_cash - edit_debt
                            fair_value = equity_value / edit_shares
                            
                            margin_of_safety = ((fair_value - current_price) / current_price) * 100
                            
                            st.markdown(f"### Intrinsic Value: **${fair_value:.2f}**")
                            if margin_of_safety > 0:
                                st.success(f"**Undervalued by {margin_of_safety:.1f}%** (The stock is cheaper than its true worth)")
                            else:
                                st.error(f"**Overvalued by {abs(margin_of_safety):.1f}%** (The stock is more expensive than its true worth)")
                                
                            with st.expander("🔍 Show Valuation Bridge (How we got this number)"):
                                st.caption("This bridge converts the value of the 'Business Operations' into the value of the 'Shares'.")
                                st.caption(f"PV of 10-Yr Cash Flows: **${sum(pv_cash_flows)/1e9:.2f}B**")
                                st.caption(f"PV of Terminal Value: **${pv_terminal_value/1e9:.2f}B**")
                                st.caption(f"Value of Operations (Enterprise Value): **${value_of_operations/1e9:.2f}B**")
                                st.caption(f"+ Non-Op Assets (Total Cash): **${edit_cash/1e9:.2f}B**")
                                st.caption(f"- Total Debt: **${edit_debt/1e9:.2f}B**")
                                st.caption(f"**= Total Value of Equity: ${equity_value/1e9:.2f}B**")
                                
                            # -------------------------------------------------------------
                            # FEATURE 2: SENSITIVITY TABLES
                            # -------------------------------------------------------------
                            st.markdown("##### Valuation Sensitivity Matrix")
                            st.caption("How does the Intrinsic Value change if we slightly tweak the WACC or Terminal Growth?")
                            
                            rates = [wacc_input - 0.02, wacc_input, wacc_input + 0.02]
                            growths = [g_lt - 0.01, g_lt, g_lt + 0.01]
                            matrix = []
                            
                            for g in growths:
                                row = []
                                for r in rates:
                                    if r > g:
                                        pv_cfs_matrix = sum([(cf * 1e9) / ((1 + r) ** (i + 1)) for i, cf in enumerate(cash_flows)])
                                        tv_matrix = (year_10_cf * (1 + g)) / (r - g)
                                        pv_tv_matrix = tv_matrix / ((1 + r) ** 10)
                                        eq_val_matrix = pv_cfs_matrix + pv_tv_matrix + edit_cash - edit_debt
                                        val_per_share = max(0, eq_val_matrix / edit_shares)
                                        row.append(f"${val_per_share:.2f}")
                                    else:
                                        row.append("N/A")
                                matrix.append(row)
                                
                            sens_df = pd.DataFrame(matrix, columns=[f"WACC {r*100:.1f}%" for r in rates], index=[f"Term. Growth {g*100:.1f}%" for g in growths])
                            st.dataframe(sens_df, use_container_width=True)

                        else:
                            st.error("Error: WACC must be strictly higher than Terminal Growth to calculate Intrinsic Value.")

                    with vc2:
                        # -------------------------------------------------------------
                        # FEATURE 3: REVERSE DCF
                        # -------------------------------------------------------------
                        st.subheader("2. Reverse DCF (Market Expectations)")
                        st.caption("Works backward from today's stock price to find out exactly what growth rate Wall Street is pricing in.")
                        
                        def solve_reverse_dcf(target_price, base_cf, shares, wacc, term_g, cash, debt):
                            low, high = -0.50, 1.50
                            implied = 0.0
                            for _ in range(60):
                                mid = (low + high) / 2
                                temp_cfs = []
                                curr_cf = base_cf
                                for i in range(1, 6):
                                    curr_cf *= (1 + mid)
                                    temp_cfs.append(curr_cf / ((1 + wacc) ** i))
                                for i in range(6, 11):
                                    curr_cf *= (1 + (mid/2))
                                    temp_cfs.append(curr_cf / ((1 + wacc) ** i))
                                
                                term_val = (curr_cf * (1 + term_g)) / (wacc - term_g)
                                pv_term = term_val / ((1 + wacc) ** 10)
                                
                                test_price = (sum(temp_cfs) + pv_term + cash - debt) / shares
                                
                                if test_price > target_price: high = mid
                                else: low = mid
                                implied = mid
                            return implied
                        
                        if wacc_input > g_lt and base_cf_input > 0:
                            implied_growth = solve_reverse_dcf(current_price, (base_cf_input * 1e9), edit_shares, wacc_input, g_lt, edit_cash, edit_debt)
                            st.info(f"To justify its current price of **${current_price:.2f}**, {val_ticker} must grow cash flows at **{implied_growth*100:.1f}%** for Yrs 1-5, and **{(implied_growth/2)*100:.1f}%** for Yrs 6-10.")
                            st.caption("💡 **How to use this:** If you believe the company can easily beat these implied growth rates, the stock is likely undervalued. If these rates seem impossibly high, it's a bubble.")
                        else:
                            st.info("Reverse DCF requires a positive base cash flow and WACC > Terminal Growth.")
                            
                        st.markdown("---")
                        
                        # -------------------------------------------------------------
                        # FEATURE 4: EPS x P/E SCENARIOS
                        # -------------------------------------------------------------
                        st.subheader("3. EPS × P/E Return Model")
                        st.caption("Predicts future price based on earnings compounding and expected market multiples.")
                        
                        pe_c1, pe_c2 = st.columns(2)
                        with pe_c1:
                            eps_input = st.number_input("Current EPS", value=float(metrics.get("eps_ttm", 0.0)), step=0.5, help="Earnings Per Share over the trailing 12 months.")
                            eps_cagr = st.number_input("Expected EPS CAGR %", value=12.0, step=1.0, help="Your expected annual growth rate for earnings.") / 100.0
                        with pe_c2:
                            years_out = st.number_input("Years to Hold", value=5, step=1, help="Your investment time horizon.")
                            target_pe = st.number_input("Target Exit P/E", value=20.0, step=1.0, help="The Price-to-Earnings multiple you believe the market will assign to this stock when you sell it.")
                            
                        future_eps = eps_input * ((1 + eps_cagr) ** years_out)
                        future_price = future_eps * target_pe
                        annualized_return = (((future_price / current_price) ** (1 / years_out)) - 1) * 100 if current_price > 0 else 0
                        
                        st.markdown(f"**Year {years_out} Projected Price:** ${future_price:.2f}")
                        st.markdown(f"**Annualized Return (CAGR):** {annualized_return:.1f}%")
                        st.caption("💡 **How to use this:** Even if earnings grow fast, if the P/E multiple contracts (e.g., drops from 50 to 20), your total return could still be negative. This model tests that risk.")
                        
                        st.markdown("---")
                        
                        # -------------------------------------------------------------
                        # FEATURE 5 & 6: FCF YIELD & BOND YIELD COMPARISON
                        # -------------------------------------------------------------
                        st.subheader("4. Yield Comparison (Risk Premium)")
                        
                        calc_yield = (base_cf_input * 1e9) / market_cap if market_cap > 0 else 0
                        equity_risk_premium = calc_yield - edit_rf
                        
                        y1, y2, y3 = st.columns(3)
                        y1.metric(f"Current {metrics.get('cf_label').split(' ')[0]} Yield", f"{calc_yield*100:.2f}%", help="How much cash the company generates relative to its market cap. Think of it like an interest rate on the stock.")
                        y2.metric("10-Yr Treasury", f"{edit_rf*100:.2f}%", help="The guaranteed, risk-free return you can get from US Government bonds.")
                        y3.metric("Equity Risk Premium", f"{equity_risk_premium*100:.2f}%", help="The excess yield you get for taking on the risk of buying this stock instead of a safe bond.")
                        
                        st.caption("💡 **How to use this:** The Risk Premium must be high enough to compensate you for the risk of owning stock. If the premium is negative, bonds are currently paying you more than this stock's underlying cash engine.")
                        
                else:
                    st.error("Could not pull reliable financial data for this ticker. Ensure it is a valid US public stock.")
            except Exception as e:
                st.error(f"Error loading valuation data. Please check your API key or try again later. ({e})")

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
import os
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime, timedelta
from google import genai
from google.genai import types

# --- 0. SUPER USERS ---
SUPER_USERS = ["boatengampomah@gmail.com", "emcheix@gmail.com"]

# --- 1. SELF-HEALING DATABASE & QUOTA LOGIC ---
def init_db():
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS leads 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, target_ticker TEXT, timestamp TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS usage_logs 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, run_timestamp TEXT, is_premium BOOLEAN, report_count INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS alerts 
                     (email TEXT, alert_type TEXT, timestamp TEXT)''')
        conn.commit()
        conn.close()
    except Exception: pass

def save_lead(email, ticker):
    init_db()
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute("INSERT INTO leads (email, target_ticker, timestamp) VALUES (?, ?, ?)", 
                  (email, ticker, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception: pass

def get_usage(email):
    init_db() 
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        forty_eight_hours_ago = (datetime.now() - timedelta(hours=48)).isoformat()
        c.execute("SELECT is_premium, report_count FROM usage_logs WHERE email=? AND run_timestamp >= ?", (email, forty_eight_hours_ago))
        rows = c.fetchall()
        conn.close()
        p_runs, p_reps, s_reps = 0, 0, 0
        for is_premium, count in rows:
            if is_premium: p_runs += 1; p_reps += count
            else: s_reps += count
        return p_runs, p_reps, s_reps
    except Exception: return 0, 0, 0

def log_usage(email, is_premium, report_count):
    init_db()
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute("INSERT INTO usage_logs (email, run_timestamp, is_premium, report_count) VALUES (?, ?, ?, ?)",
                  (email, datetime.now().isoformat(), is_premium, report_count))
        conn.commit()
        conn.close()
    except Exception: pass

def send_limit_email(email, limit_msg):
    init_db()
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        twenty_four_hours_ago = (datetime.now() - timedelta(hours=24)).isoformat()
        c.execute("SELECT COUNT(*) FROM alerts WHERE email=? AND alert_type='limit' AND timestamp >= ?", (email, twenty_four_hours_ago))
        if c.fetchone()[0] == 0:
            try:
                msg = MIMEMultipart()
                msg['From'] = f"B.E Research <{st.secrets['EMAIL_SENDER']}>"
                msg['To'] = email
                msg['Subject'] = "Usage Limit Reached"
                msg.attach(MIMEText(f"Hello,\n\nYou reached a limit: {limit_msg}\n\nBest,\nB.E Research Team", 'plain'))
                server = smtplib.SMTP("smtp.gmail.com", 587); server.starttls()
                server.login(st.secrets['EMAIL_SENDER'], st.secrets['EMAIL_PASSWORD']); server.send_message(msg); server.quit()
                c.execute("INSERT INTO alerts (email, alert_type, timestamp) VALUES (?, ?, ?)", (email, 'limit', datetime.now().isoformat())); conn.commit()
            except Exception: pass
        conn.close()
    except Exception: pass

# --- 2. APP SETUP ---
st.set_page_config(page_title="B.E Research Investing Assistant", page_icon="📈", layout="wide")

@st.cache_resource
def get_task_registry(): return {} 
@st.cache_resource
def get_executor(): return concurrent.futures.ThreadPoolExecutor(max_workers=2)

global_tasks = get_task_registry()
background_executor = get_executor()

if "auto_ceo" not in st.session_state: st.session_state.auto_ceo = ""
if "auto_company" not in st.session_state: st.session_state.auto_company = ""

def fetch_info_from_ticker():
    ticker = st.session_state.ticker_input.strip()
    if ticker:
        try:
            stock = yf.Ticker(ticker); info = stock.info
            if 'longName' in info: st.session_state.auto_company = info.get('longName', '')
            for officer in info.get('companyOfficers', []):
                if 'CEO' in officer.get('title', '').upper(): st.session_state.auto_ceo = officer.get('name'); break
        except Exception: pass

# --- 3. PROMPTS ---
gem_prompts = {
    # --- DEPENDENT AGENTS (SYNTHESIS) ---
    "Company - Financial Trajectory & Macro Sensitivity": """ROLE: You are a quantitative fundamental analyst.
Using the provided financial data, market context, and historical performance, analyze the financial engine of [STOCK NAME] ([TICKER]). 
TASKS:
1. FINANCIAL TRAJECTORY: Analyze the 3-5 year trend for Revenue, Gross Margins, Operating Margins (EBIT), and Net Income. Are margins expanding or compressing? Why?
2. CASH & CAPITAL ALLOCATION: Evaluate Free Cash Flow (FCF) generation. How is management deploying capital? (Are they hoarding cash, paying dividends, buying back shares, or aggressively doing M&A/Capex?)
3. MACRO SENSITIVITY (The "Macro" in Macro Understanding): Explicitly define how sensitive [STOCK NAME] is to current macroeconomic factors. 
- Interest Rates: How does the cost of capital affect their debt load or customer demand?
- Inflation/Pricing Power: Can they pass rising costs to consumers without losing volume?
- FX/Geopolitics: What is their exposure to currency fluctuations or supply chain shocks?
4. ROIC & EFFICIENCY: Assess their Return on Invested Capital (ROIC) vs their Weighted Average Cost of Capital (WACC) if data allows. Are they actually creating value, or just growing for the sake of growth?
OUTPUT FORMAT: Use heavy formatting, bullet points, and bold text for readability. Output the *story* those numbers tell. End with a 1-sentence "Financial Health Verdict".""",

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
VERDICT: Flag as GREEN (Clean), YELLOW (Aggressive/Watch), or RED (Short Candidate). Detail the precise mechanism that would trigger a collapse."""
}

dependent_agents = ["Company - Financial Trajectory & Macro Sensitivity", "Company - Final Investment Memo & Rating"]
industry_agents = [k for k in gem_prompts.keys() if "Industry" in k]
concept_agents = ["Concept - Investment Education & Metric Breakdown"]
ceo_agents = ["CEO - Track Record & Capital Allocation"]
stock_base_agents = [k for k in gem_prompts.keys() if k not in (dependent_agents + industry_agents + concept_agents + ceo_agents)]

# --- 4. UI ---
st.title("📈 B.E Research Investing Assistant")
st.markdown("**Wall Street-level research, simplified for every investor.**")
st.warning("⚖️ **Disclaimer:** Not financial advice. For educational purposes only.")

with st.sidebar:
    st.header("🔐 Admin Dashboard")
    auth = st.text_input("Password", type="password")
    if auth == st.secrets.get("ADMIN_PASSWORD", ""):
        try:
            conn = sqlite3.connect('users.db'); df = pd.read_sql_query("SELECT * FROM leads ORDER BY id DESC", conn)
            st.dataframe(df); conn.close()
        except Exception: pass

st.markdown("### Step 1: Target Information")
user_email = st.text_input("📧 Email for delivery:", placeholder="yourname@example.com")
user_email_clean = user_email.strip().lower()

if user_email_clean and "@" in user_email_clean and user_email_clean not in SUPER_USERS:
    p_runs, _, s_reps = get_usage(user_email_clean)
    st.info(f"⏳ **48hr Remaining:** {max(0, 4-p_runs)} Premium Runs | {max(0, 30-s_reps)} Standard Reports")

col1, col2 = st.columns(2)
with col1:
    t_company = st.text_input("Company Name (e.g. Tesla):", value=st.session_state.auto_company)
    t_ticker = st.text_input("Ticker Symbol (e.g. TSLA):", key="ticker_input", on_change=fetch_info_from_ticker)
    t_concept = st.text_input("Financial Concept to Explain (e.g. ROIC):")
with col2:
    t_industry = st.text_input("Industry Sector (e.g. Electric Vehicles):")
    t_ceo = st.text_input("CEO's Name:", value=st.session_state.auto_ceo)

st.markdown("---")
st.markdown("### Step 2: Engine Configuration")
cfg1, cfg2 = st.columns(2)
with cfg1:
    brain_id = st.radio("🧠 Engine Power:", ["gemini-3.1-flash-lite-preview", "gemini-3.1-pro-preview"], format_func=lambda x: "Fast (Standard)" if "flash" in x else "High (Advanced)", index=0)
with cfg2:
    tool_id = st.radio("🔎 Method:", ["Standard Search", "Deep Research", "Market Data"], index=0)

st.markdown("---")
st.markdown("### Step 3: Select Reports")
st.info("💡 You can select multiple reports at once for a comprehensive analysis.")
selected = st.multiselect("📑 Reports to generate:", list(gem_prompts.keys()), default=[])

# --- 5. BACKGROUND ENGINE ---
def execute_background_job(email, ticker, company, industry, ceo, concept, prompts_to_run, brain_id, tool_id, api_key, email_sender, email_pwd, is_premium):
    client = genai.Client(api_key=api_key); reports = {}
    
    # Resolver
    resolved_ticker = ticker.strip().upper()
    resolved_company = company.strip()
    if resolved_company and not resolved_ticker:
        try:
            res = client.models.generate_content(model='gemini-3.1-flash-lite-preview', contents=f"Ticker for {resolved_company}? Symbol only.")
            resolved_ticker = res.text.strip().upper()
        except: pass

    def fire(n, raw_instr, ctx=""):
        instr = raw_instr.replace("[STOCK NAME]", resolved_company).replace("[TICKER]", resolved_ticker).replace("[INSERT INDUSTRY]", industry).replace("[Industry Name]", industry).replace("{{CEO Name}}", ceo).replace("{{Company Name}}", resolved_company).replace("{CONCEPT NAME}", concept).replace("[Company_name]", resolved_company).replace("{Company_Name}", resolved_company).replace("[Company Name]", resolved_company).replace("[Insert stock]", resolved_ticker)
        instr += "\n\nCRITICAL: Be exhaustive (1500+ words). List 'SOURCES & REFERENCES' at the bottom. This is a B.E Research product."
        try:
            if ctx and n in dependent_agents:
                res = client.models.generate_content(model='gemini-3.1-pro-preview', contents=f"SYNTHESIS:\n{instr}\n\nDATA:\n{ctx}")
                return n, res.text
            if tool_id == "Deep Research":
                it = client.interactions.create(agent='deep-research-pro-preview-12-2025', input=instr, background=True)
                while True:
                    it = client.interactions.get(it.id)
                    if it.status == "completed": return n, it.outputs[-1].text
                    time.sleep(10)
            else:
                res = client.models.generate_content(model=brain_id, contents=instr, config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]) if tool_id == "Standard Search" else None)
                return n, res.text
        except Exception as e: return n, f"Error: {e}"

    # Logic
    base_run = set(prompts_to_run)
    if any(p in dependent_agents for p in prompts_to_run): base_run.update(stock_base_agents)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fire, n, gem_prompts[n]): n for n in base_run if n not in dependent_agents}
        for f in concurrent.futures.as_completed(futs): n, t = f.result(); reports[n] = t

    dep_run = [p for p in prompts_to_run if p in dependent_agents]
    if dep_run:
        ctx = "\n\n".join([f"==={k}===\n{v}" for k,v in reports.items() if k in stock_base_agents])
        for n in dep_run: _, t = fire(n, gem_prompts[n], ctx); reports[n] = t

    final = {k: v for k, v in reports.items() if k in prompts_to_run}
    zip_b = io.BytesIO()
    with zipfile.ZipFile(zip_b, "w") as zf:
        for n, t in final.items(): zf.writestr(f"{resolved_ticker}_{n}.doc", f"<html><body>{markdown.markdown(t)}</body></html>".encode('utf-8'))
    
    try:
        msg = MIMEMultipart(); msg['From'] = f"B.E Research <{email_sender}>"; msg['To'] = email; msg['Subject'] = f"🚀 Research: {resolved_company}"
        msg.attach(MIMEText(f"B.E Research for {resolved_company} is attached.", 'plain'))
        part = MIMEBase('application', 'octet-stream'); part.set_payload(zip_b.getvalue()); encoders.encode_base64(part)
        part.add_header('Content-Disposition', f"attachment; filename={resolved_ticker}_Research.zip"); msg.attach(part)
        server = smtplib.SMTP("smtp.gmail.com", 587); server.starttls(); server.login(email_sender, email_pwd); server.send_message(msg); server.quit()
    except: pass
    global_tasks[email].update({"status": "complete", "reports": final, "zip": zip_b.getvalue()})

# --- 6. GENERATE BUTTON & BLOCKERS ---
if st.button("🚀 Generate B.E Research Package", use_container_width=True):
    if not user_email or "@" not in user_email: st.error("Email required."); st.stop()
    if not selected: st.error("Select at least one report."); st.stop()
    
    # HARD BLOCKERS ENGINE
    missing = []
    if any(p in stock_base_agents + dependent_agents for p in selected) and not (t_company or t_ticker): missing.append("Company Name or Ticker Symbol")
    if any(p in industry_agents for p in selected) and not t_industry: missing.append("Industry Sector")
    if any(p in ceo_agents for p in selected) and not t_ceo: missing.append("CEO's Name")
    if any(p in concept_agents for p in selected) and not t_concept: missing.append("Financial Concept")
    
    if missing:
        st.error(f"🛑 **Action Required:** Please provide the following to generate the selected reports: {', '.join(missing)}")
        st.stop()

    is_p = (brain_id == "gemini-3.1-pro-preview" or tool_id == "Deep Research")
    if user_email_clean not in SUPER_USERS:
        p_r, _, s_r = get_usage(user_email_clean)
        if is_p and p_r >= 4: st.error("Premium limit reached."); st.stop()
        if not is_p and s_r + len(selected) > 30: st.error("Standard limit reached."); st.stop()

    log_usage(user_email_clean, is_p, len(selected)); save_lead(user_email_clean, t_ticker)
    global_tasks[user_email_clean] = {"status": "running", "ticker": t_ticker, "count": len(selected)}
    background_executor.submit(execute_background_job, user_email_clean, t_ticker, t_company, t_industry, t_ceo, t_concept, selected, brain_id, tool_id, st.secrets["GOOGLE_API_KEY"], st.secrets["EMAIL_SENDER"], st.secrets["EMAIL_PASSWORD"], is_p)

if user_email_clean in global_tasks:
    task = global_tasks[user_email_clean]
    if task["status"] == "running":
        est = (task["count"] * 35) / 60
        st.info(f"⏳ **B.E Research Engine is working...** Est. time: ~{est:.1f} min.")
        bar = st.progress(0)
        for i in range(100): time.sleep(est * 0.15); bar.progress(i + 1)
        st.rerun()
    elif task["status"] == "complete":
        st.success("✅ Research complete! Files emailed and available below.")
        st.download_button("📥 Download Research ZIP", task["zip"], f"{task['ticker']}_BEResearch.zip", "application/zip", use_container_width=True)
        for n, t in task["reports"].items():
            with st.expander(f"View: {n}"): st.markdown(t)

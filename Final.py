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
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      email TEXT, 
                      target_ticker TEXT,
                      timestamp TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS usage_logs 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      email TEXT, 
                      run_timestamp TEXT,
                      is_premium BOOLEAN,
                      report_count INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS alerts 
                     (email TEXT, alert_type TEXT, timestamp TEXT)''')
        conn.commit()
        conn.close()
    except Exception:
        pass

def save_lead(email, ticker):
    init_db()
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute("INSERT INTO leads (email, target_ticker, timestamp) VALUES (?, ?, ?)", 
                  (email, ticker, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_usage(email):
    init_db() 
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        forty_eight_hours_ago = (datetime.now() - timedelta(hours=48)).isoformat()
        
        c.execute("SELECT is_premium, report_count FROM usage_logs WHERE email=? AND run_timestamp >= ?", 
                  (email, forty_eight_hours_ago))
        rows = c.fetchall()
        conn.close()
        
        p_runs = 0
        p_reps = 0
        s_reps = 0
        
        for is_premium, count in rows:
            if is_premium:
                p_runs += 1
                p_reps += count
            else:
                s_reps += count
                
        return p_runs, p_reps, s_reps
    except Exception:
        return 0, 0, 0

def log_usage(email, is_premium, report_count):
    init_db()
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute("INSERT INTO usage_logs (email, run_timestamp, is_premium, report_count) VALUES (?, ?, ?, ?)",
                  (email, datetime.now().isoformat(), is_premium, report_count))
        conn.commit()
        conn.close()
    except Exception:
        pass

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
                msg['Subject'] = "Action Required: B.E Research Usage Limit Reached"
                body = f"Hello,\n\nYou have reached a usage limit on the platform.\n\nDETAIL: {limit_msg}\n\nPlease wait until your 48-hour rolling window resets to generate more reports.\n\nBest,\nB.E Research Team"
                msg.attach(MIMEText(body, 'plain'))
                
                server = smtplib.SMTP("smtp.gmail.com", 587)
                server.starttls()
                server.login(st.secrets['EMAIL_SENDER'], st.secrets['EMAIL_PASSWORD'])
                server.send_message(msg)
                server.quit()
                
                c.execute("INSERT INTO alerts (email, alert_type, timestamp) VALUES (?, ?, ?)", (email, 'limit', datetime.now().isoformat()))
                conn.commit()
            except Exception:
                pass
        conn.close()
    except Exception:
        pass

# --- 2. SET UP THE WEB PAGE ---
st.set_page_config(page_title="B.E Research Investing Assistant", page_icon="📈", layout="wide")

# --- 3. PERSISTENT BACKGROUND ENGINE ---
@st.cache_resource
def get_task_registry():
    return {} 

@st.cache_resource
def get_executor():
    return concurrent.futures.ThreadPoolExecutor(max_workers=2)

global_tasks = get_task_registry()
background_executor = get_executor()

# --- INITIALIZE MEMORY FOR UI AUTO-FETCH ---
if "final_reports" not in st.session_state:
    st.session_state.final_reports = {}
if "analysis_complete" not in st.session_state:
    st.session_state.analysis_complete = False
if "auto_ceo" not in st.session_state:
    st.session_state.auto_ceo = ""
if "auto_company" not in st.session_state:
    st.session_state.auto_company = ""

# --- THE CLEAN UI AUTO-FETCH LOGIC ---
def fetch_info_from_ticker():
    ticker = st.session_state.ticker_input.strip()
    if ticker:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            
            if 'longName' in info:
                st.session_state.auto_company = info.get('longName', '')
            
            officers = info.get('companyOfficers', [])
            found_ceo = False
            for officer in officers:
                title = officer.get('title', '').upper()
                if 'CEO' in title or 'CHIEF EXECUTIVE' in title:
                    st.session_state.auto_ceo = officer.get('name')
                    found_ceo = True
                    break
            if not found_ceo:
                st.session_state.auto_ceo = ""
                
        except Exception:
            pass

# --- 4. INSTITUTIONAL PROMPT LIBRARY ---
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

dependent_agents = [
    "Company - Financial Trajectory & Macro Sensitivity", 
    "Company - Final Investment Memo & Rating"
]
industry_agents = [
    "Industry - Macro Environment & Strategic Outlook", 
    "Industry - Future Growth & Disruption Scenarios", 
    "Industry - Core Economics & Market Structure", 
    "Industry - Business Models & Ecosystem Architecture", 
    "Industry - Value Chain Mapping & Key Players", 
    "Industry - Unit Economics & Operating Leverage", 
    "Industry - Geopolitics, Regulation & TAM"
]
concept_agents = ["Concept - Investment Education & Metric Breakdown"]
ceo_agents = ["CEO - Track Record & Capital Allocation"]

stock_base_agents = [k for k in gem_prompts.keys() if k not in dependent_agents + industry_agents + concept_agents + ceo_agents]

# --- MAIN UI SETUP ---
st.title("📈 B.E Research Investing Assistant")
st.markdown("Wall Street-level stock and industry research, explained simply for everyday investors.")
st.caption("⚠️ **Disclaimer:** The reports generated are for educational and informational purposes only and do not constitute financial, investment, or legal advice. Always conduct your own research or consult with a certified financial advisor before making any investment decisions.")

# --- 5. ADMIN SIDEBAR ---
with st.sidebar:
    st.header("🔐 Admin Dashboard")
    st.info("Regular users do not need to access this menu.")
    auth_pass = st.text_input("Admin Password", type="password")
    if auth_pass == st.secrets.get("ADMIN_PASSWORD", ""):
        st.success("Authenticated")
        try:
            conn = sqlite3.connect('users.db')
            df = pd.read_sql_query("SELECT * FROM leads ORDER BY id DESC", conn)
            st.dataframe(df, use_container_width=True)
            st.download_button("📥 Export CSV", df.to_csv(index=False), "beresearch_leads.csv", "text/csv")
            conn.close()
        except Exception:
            st.error("Database initializing...")

# --- 6. MAIN UI FLOW ---
st.markdown("### Step 1: Target Information")
user_email = st.text_input("📧 Enter your email to receive the final report ZIP:")
user_email_clean = user_email.strip().lower()

is_super_user = user_email_clean in SUPER_USERS
if user_email_clean and "@" in user_email_clean:
    if is_super_user:
        st.success("🌟 Super User Access: Unlimited Reports Available")
    else:
        p_runs, p_reps, s_reps = get_usage(user_email_clean)
        rem_p_runs = max(0, 4 - p_runs)
        rem_p_reps = max(0, 6 - p_reps)
        rem_s_reps = max(0, 30 - s_reps)
        
        st.markdown("##### ⏳ Your 48-Hour Quota Remaining")
        q1, q2, q3 = st.columns(3)
        q1.metric("Premium Runs", f"{rem_p_runs} / 4")
        q2.metric("Premium Reports", f"{rem_p_reps} / 6")
        q3.metric("Standard Reports", f"{rem_s_reps} / 30")

col1, col2 = st.columns(2)
with col1:
    target_company = st.text_input("Company Name (e.g., Tesla):", value=st.session_state.auto_company)
    target_ticker = st.text_input("Ticker Symbol (e.g., TSLA):", key="ticker_input", on_change=fetch_info_from_ticker)
    target_concept = st.text_input("Financial Concept to Explain (Optional, e.g., ROIC):")
with col2:
    target_industry = st.text_input("Industry (e.g., Electric Vehicles):")
    target_ceo = st.text_input("CEO's Name (Optional):", value=st.session_state.auto_ceo)

st.markdown("---")
st.markdown("### Step 2: Engine Configuration")

cfg_col1, cfg_col2 = st.columns(2)
with cfg_col1:
    brain_options = {
        "Gemini 3.1 Pro (High Reasoning)": "gemini-3.1-pro-preview",
        "Gemini 3.1 Flash Lite (Fast/Cheap)": "gemini-3.1-flash-lite-preview"
    }
    selected_brain_label = st.radio("🧠 Model Engine:", list(brain_options.keys()))
    selected_brain = brain_options[selected_brain_label]

with cfg_col2:
    tool_choice = st.radio("🔎 Grounding Method:", ["Standard Google Search", "Deep Research", "Yahoo Finance Data"])

st.markdown("---")
st.markdown("### Step 3: Select Reports")
selected_prompts = st.multiselect("📑 Choose specific research reports to generate:", list(gem_prompts.keys()), default=list(gem_prompts.keys()))
st.markdown("---")

# --- 7. THE BACKGROUND WORKER (THE ROUTING ENGINE) ---
def execute_background_job(email, ticker, company, industry, ceo, concept, prompts_to_run, brain_id, tool_id, api_key, email_sender, email_pwd, is_premium_run):
    global_tasks[email]["progress"] = "Initializing & Auto-Resolving Missing Data..."
    client = genai.Client(api_key=api_key)
    reports = {}
    
    resolved_ticker = ticker.strip().upper()
    resolved_company = company.strip()
    resolved_ceo = ceo.strip()

    if resolved_company and not resolved_ticker:
        try:
            prompt = f"What is the official stock ticker symbol for '{resolved_company}'? Return ONLY the symbol (e.g., TSLA). If it is a private company, reply PRIVATE."
            res = client.models.generate_content(model='gemini-3.1-flash-lite-preview', contents=prompt)
            ans = res.text.strip().replace("$", "").upper()
            if "PRIVATE" not in ans and len(ans) <= 10:
                resolved_ticker = ans
        except:
            pass

    if resolved_ticker:
        try:
            stock = yf.Ticker(resolved_ticker)
            info = stock.info
            if not resolved_company:
                resolved_company = info.get('longName', resolved_ticker)
            if not resolved_ceo:
                officers = info.get('companyOfficers', [])
                for officer in officers:
                    title = officer.get('title', '').upper()
                    if 'CEO' in title or 'CHIEF EXECUTIVE' in title:
                        resolved_ceo = officer.get('name')
                        break
        except:
            pass

    if not resolved_company: resolved_company = resolved_ticker if resolved_ticker else "the company"
    if not resolved_ticker: resolved_ticker = resolved_company
    if not resolved_ceo: resolved_ceo = "the CEO"

    yf_context = ""
    if tool_id == "Yahoo Finance":
        global_tasks[email]["progress"] = f"Fetching Yahoo Finance data for {resolved_ticker}..."
        try:
            stock = yf.Ticker(resolved_ticker)
            info = stock.info
            yf_context = f"BUSINESS SUMMARY:\n{info.get('longBusinessSummary', 'N/A')}\n\n"
            yf_context += f"FINANCIALS:\n{stock.financials.head(15).to_string()}\n"
        except Exception as e:
            yf_context = f"Could not fetch YFinance data: {e}"

    def fire_agent(agent_name, raw_instruction, extra_context=""):
        if agent_name in industry_agents and not industry.strip(): return agent_name, "Skipped: No Industry provided."
        if agent_name in concept_agents and not concept.strip(): return agent_name, "Skipped: No Concept provided."
        if agent_name in ceo_agents and not resolved_ceo.strip(): return agent_name, "Skipped: No CEO found or provided."

        instruction = raw_instruction.replace("[STOCK NAME]", resolved_company) \
                                     .replace("[TICKER]", resolved_ticker) \
                                     .replace("[Company_name]", resolved_company) \
                                     .replace("[company_name]", resolved_company) \
                                     .replace("[Company Name]", resolved_company) \
                                     .replace("{Company_Name}", resolved_company) \
                                     .replace("{{Company Name}}", resolved_company) \
                                     .replace("[COMPANY]", resolved_company) \
                                     .replace("{{CEO Name}}", resolved_ceo) \
                                     .replace("[INSERT INDUSTRY]", industry) \
                                     .replace("[INSERT INDUSTRY NAME]", industry) \
                                     .replace("[Industry Name]", industry) \
                                     .replace("[Insert Industry Name]", industry) \
                                     .replace("[Insert stock]", resolved_ticker) \
                                     .replace("{CONCEPT NAME}", concept)
        
        instruction += "\n\nCRITICAL INSTRUCTION: Be absolutely exhaustive, highly analytical, and highly descriptive. Do not write high-level summaries. Dive deep into the raw data, explicitly cite metrics, and write at least 1,500 to 2,500 words for this specific report. MANDATORY: At the very bottom of your report, include a 'SOURCES & REFERENCES' section listing every document, financial filing, or dataset you used to generate these findings."

        try:
            if extra_context and agent_name in dependent_agents:
                prompt = f"YOU ARE A SYNTHESIS AGENT. USE THE RESEARCH BELOW:\n\n{instruction}\n\nRESEARCH DATA:\n{extra_context}"
                res = client.models.generate_content(model='gemini-3.1-pro-preview', contents=prompt, config=types.GenerateContentConfig(temperature=0.1))
                return agent_name, res.text
            
            if tool_id == "Deep Research":
                interaction = client.interactions.create(agent='deep-research-pro-preview-12-2025', input=instruction, background=True)
                while True:
                    interaction = client.interactions.get(interaction.id)
                    if interaction.status == "completed": return agent_name, interaction.outputs[-1].text
                    if interaction.status == "failed": return agent_name, f"Deep Research Error: {interaction.error}"
                    time.sleep(15)
            
            elif tool_id == "Yahoo Finance":
                prompt = f"{instruction}\n\nMARKET DATA CONTEXT:\n{yf_context}"
                res = client.models.generate_content(model=brain_id, contents=prompt)
                return agent_name, res.text
            
            else: 
                res = client.models.generate_content(
                    model=brain_id, 
                    contents=instruction, 
                    config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
                )
                return agent_name, res.text

        except Exception as e:
            return agent_name, f"Error: {e}"

    base_prompts_to_run = set([p for p in prompts_to_run if p not in dependent_agents])
    dep_prompts_to_run = [p for p in prompts_to_run if p in dependent_agents]

    if dep_prompts_to_run:
        base_prompts_to_run.update(stock_base_agents)

    if base_prompts_to_run:
        global_tasks[email]["progress"] = f"Stage 1: Gathering necessary research data..."
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_agent = {executor.submit(fire_agent, name, gem_prompts[name]): name for name in base_prompts_to_run}
            for future in concurrent.futures.as_completed(future_to_agent):
                name, text = future.result()
                if text: reports[name] = text

    if dep_prompts_to_run:
        global_tasks[email]["progress"] = "Stage 2: Synthesizing final thesis..."
        aggregated_context = "\n\n".join([f"=== {k} ===\n{v}" for k, v in reports.items() if "Skipped" not in v and "Error" not in v and k in stock_base_agents])
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_to_agent = {executor.submit(fire_agent, name, gem_prompts[name], aggregated_context): name for name in dep_prompts_to_run}
            for future in concurrent.futures.as_completed(future_to_agent):
                name, text = future.result()
                if text: reports[name] = text

    final_user_reports = {k: v for k, v in reports.items() if k in prompts_to_run}

    global_tasks[email]["progress"] = "Compiling ZIP and sending email..."
    global_tasks[email]["reports"] = final_user_reports
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for name, text in final_user_reports.items():
            safe_name = name.replace(" ", "_").replace("/", "-")
            html_content = markdown.markdown(text, extensions=['tables'])
            doc_content = f"<html><head><meta charset='utf-8'></head><body>{html_content}</body></html>"
            zip_file.writestr(f"{resolved_ticker}_{safe_name}.doc", doc_content.encode('utf-8'))
    
    warning_msg = ""
    is_super = email in SUPER_USERS
    if not is_super:
        p_runs, p_reps, s_reps = get_usage(email)
        if is_premium_run and (p_runs >= 4 or p_reps >= 6):
            warning_msg = "\n\n⚠️ NOTE: You have exhausted your maximum limit for Premium features. You will not be able to run Deep Research or Gemini Pro for the next 48 hours."
        elif not is_premium_run and s_reps >= 30:
            warning_msg = "\n\n⚠️ NOTE: You have exhausted your maximum limit for Standard features. You will not be able to generate standard reports for the next 48 hours."

    try:
        msg = MIMEMultipart()
        msg['From'] = f"B.E Research <{email_sender}>"
        msg['To'] = email
        msg['Subject'] = f"🚀 Analysis Complete: {resolved_company}"
        body = f"Your specific requested research for {resolved_company} is attached.{warning_msg}"
        msg.attach(MIMEText(body, 'plain'))
        
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(zip_buffer.getvalue())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f"attachment; filename={resolved_ticker}_BEResearch_Reports.zip")
        msg.attach(part)
        
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(email_sender, email_pwd)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"Email failed: {e}")

    global_tasks[email]["status"] = "complete"

# --- 8. RUN BUTTON & BILLING GATEKEEPER ---
if st.button("🚀 Generate B.E Research Report", use_container_width=True):
    if not user_email or "@" not in user_email:
        st.error("Please enter a valid email address.")
        st.stop()
        
    if not selected_prompts:
        st.error("Please select at least one report to generate.")
        st.stop()

    needs_stock = any(p in stock_base_agents + dependent_agents for p in selected_prompts)
    needs_industry = any(p in industry_agents for p in selected_prompts)
    needs_ceo = any(p in ceo_agents for p in selected_prompts)
    needs_concept = any(p in concept_agents for p in selected_prompts)

    if needs_stock and not target_company.strip() and not target_ticker.strip():
        st.error("Please provide either a Company Name OR a Ticker Symbol.")
        st.stop()
    if needs_industry and not target_industry.strip():
        st.error("One or more selected reports require an Industry Sector.")
        st.stop()
    if needs_ceo and not target_company.strip() and not target_ticker.strip():
        st.error("The CEO report needs to know the company. Please provide a Company Name or Ticker Symbol.")
        st.stop()
    if needs_concept and not target_concept.strip():
        st.error("The Concept Education report requires a Financial Concept.")
        st.stop()

    is_premium_request = (selected_brain == "gemini-3.1-pro-preview" or tool_choice == "Deep Research")
    num_requested = len(selected_prompts)
    
    if not is_super_user:
        p_runs, p_reps, s_reps = get_usage(user_email_clean)
        
        if is_premium_request:
            if p_runs >= 4:
                msg = "You have exhausted your 4 Premium runs for the last 48 hours."
                st.error(f"🛑 {msg}")
                send_limit_email(user_email_clean, msg)
                st.stop()
            if p_reps + num_requested > 6:
                rem = max(0, 6 - p_reps)
                msg = f"You requested {num_requested} Premium reports, but only have {rem} remaining for the next 48 hours."
                st.error(f"🛑 {msg}")
                send_limit_email(user_email_clean, msg)
                st.stop()
        else:
            if s_reps + num_requested > 30:
                rem = max(0, 30 - s_reps)
                msg = f"You requested {num_requested} Standard reports, but only have {rem} remaining for the next 48 hours."
                st.error(f"🛑 {msg}")
                send_limit_email(user_email_clean, msg)
                st.stop()
                
    log_usage(user_email_clean, is_premium_request, num_requested)

    safe_ticker_for_file = target_ticker.strip().upper() if target_ticker.strip() else "General_Report"
    save_lead(user_email_clean, safe_ticker_for_file)
    
    global_tasks[user_email_clean] = {"status": "running", "progress": "Starting...", "reports": {}, "ticker": safe_ticker_for_file}
    
    background_executor.submit(
        execute_background_job, 
        user_email_clean, target_ticker, target_company, target_industry, target_ceo, target_concept, 
        selected_prompts, selected_brain, tool_choice, 
        st.secrets["GOOGLE_API_KEY"], st.secrets["EMAIL_SENDER"], st.secrets["EMAIL_PASSWORD"], is_premium_request
    )

# --- 9. UI STATE DISPLAY ---
if user_email_clean in global_tasks:
    task = global_tasks[user_email_clean]
    
    if task["status"] == "running":
        st.info(f"⏳ **Running:** {task['progress']}")
        st.caption("You can safely refresh this page or close the tab. The task is running in the background and will be emailed to you.")
        time.sleep(3)
        st.rerun()
        
    elif task["status"] == "complete":
        st.success("✅ Analysis Complete! Check your email inbox.")
        
        st.header("📑 Your Reports")
        for name, text in task["reports"].items():
            with st.expander(f"View Report: {name}"):
                st.markdown(text)
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for name, text in task["reports"].items():
                safe_name = name.replace(" ", "_").replace("/", "-")
                html_content = markdown.markdown(text, extensions=['tables'])
                doc_content = f"<html><head><meta charset='utf-8'></head><body>{html_content}</body></html>"
                zip_file.writestr(f"{task['ticker']}_{safe_name}.doc", doc_content.encode('utf-8'))
        
        st.download_button(
            label="📥 Download Reports as .ZIP",
            data=zip_buffer.getvalue(),
            file_name=f"{task['ticker']}_BEResearch_Reports.zip",
            mime="application/zip",
            use_container_width=True
        )

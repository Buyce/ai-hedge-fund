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
        
        p_runs, p_reps, s_reps = 0, 0, 0
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
                msg['From'] = f"B.E Research Investing Assistant <{st.secrets['EMAIL_SENDER']}>"
                msg['To'] = email
                msg['Subject'] = "Action Required: B.E Research Usage Limit Reached"
                body = f"Hello,\n\nYou have reached a usage limit on the B.E Research platform.\n\nDETAIL: {limit_msg}\n\nPlease wait until your 48-hour rolling window resets.\n\nBest,\nB.E Research Team"
                msg.attach(MIMEText(body, 'plain'))
                server = smtplib.SMTP("smtp.gmail.com", 587)
                server.starttls()
                server.login(st.secrets['EMAIL_SENDER'], st.secrets['EMAIL_PASSWORD'])
                server.send_message(msg)
                server.quit()
                c.execute("INSERT INTO alerts (email, alert_type, timestamp) VALUES (?, ?, ?)", (email, 'limit', datetime.now().isoformat()))
                conn.commit()
            except Exception: pass
        conn.close()
    except Exception: pass

# --- 2. SETUP & STATE ---
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
            stock = yf.Ticker(ticker)
            info = stock.info
            if 'longName' in info: st.session_state.auto_company = info.get('longName', '')
            for officer in info.get('companyOfficers', []):
                if 'CEO' in officer.get('title', '').upper():
                    st.session_state.auto_ceo = officer.get('name')
                    break
        except Exception: pass

# --- 3. B.E RESEARCH PROMPT LIBRARY ---
gem_prompts = {
    "Company - Financial Trajectory & Macro Sensitivity": """ROLE: You are a quantitative fundamental analyst. Analyze the financial engine of [STOCK NAME] ([TICKER]). 
1. FINANCIAL TRAJECTORY: Analyze trend for Revenue, Gross Margins, Operating Margins (EBIT), and Net Income. 
2. CASH & CAPITAL ALLOCATION: Evaluate Free Cash Flow (FCF) generation. 
3. MACRO SENSITIVITY: Define how sensitive the company is to Interest Rates and Inflation.""",

    "Company - Final Investment Memo & Rating": """ROLE: You are the Lead Portfolio Manager. Synthesize all provided research into a final investment memo for [STOCK NAME] ([TICKER]).
1. INVESTMENT RATING: STRONG BUY / BUY / HOLD / SELL / AVOID. 
2. CORE THESIS: What is the market mispricing? 
3. CATALYSTS: 2-3 specific events in 6-12 months.""",

    "Industry - Macro Environment & Strategic Outlook": """Produce a data-driven sector intelligence report for [INSERT INDUSTRY]. Focus on regime changes, capital flows, and structural shifts.""",

    "Industry - Future Growth & Disruption Scenarios": """Project the 5-10 year future for [Industry Name]. Analyze cost declines and technological convergence.""",

    "Industry - Core Economics & Market Structure": """Provide an overview of the [Insert Industry Name] industry. Focus on unit economics and profit pool concentration.""",

    "Industry - Business Models & Ecosystem Architecture": """Decode the underlying business architecture and power dynamics of [INSERT INDUSTRY NAME].""",

    "Industry - Value Chain Mapping & Key Players": """Map the [Industry Name] value chain using 5-10 publicly traded companies.""",

    "Industry - Unit Economics & Operating Leverage": """Deconstruct the operational mechanics and unit economics of [Industry Name].""",

    "Industry - Geopolitics, Regulation & TAM": """Map the structural macro dynamics and Total Addressable Market for [INSERT INDUSTRY NAME].""",

    "Concept - Investment Education & Metric Breakdown": """Deconstruct the concept of {CONCEPT NAME} for a smart investor. Define it simply, show the mechanics, and explain how it is used or manipulated by management.""",

    "CEO - Track Record & Capital Allocation": """Produce an evidence-based dossier on {{CEO Name}} at {{Company Name}}. Evaluate past capital allocation and promises vs. execution.""",

    "Company - Management Quality & Insider Incentives": """Ruthless evaluation of [Company_name]'s management alignment with minority shareholders.""",

    "Company - Moat Analysis & Competitive Dynamics (7 Powers)": """Evaluate [company_name] through the lens of economic moats and Hamilton Helmer's 7 Powers.""",

    "Company - Warren Buffett Financial Statement Breakdown": """Analyze financials of {Company_Name} focusing on Owner's Earnings and downside protection.""",

    "Company - Revenue Decomposition & Organic Growth": """Deconstruct the revenue growth of [Insert stock]. Separate Price Increases from Volume Growth.""",

    "Company - Deep Business Model & Buyout Due Diligence": """Deep-dive due diligence on [Company_name] as if acquiring 100% of the equity.""",

    "Company - Forensic Accounting & Solvency Risk": """Tear apart the financials of [Company Name]. Look for manipulation and aggressive accounting."""
}

dependent_agents = ["Company - Financial Trajectory & Macro Sensitivity", "Company - Final Investment Memo & Rating"]
industry_agents = ["Industry - Macro Environment & Strategic Outlook", "Industry - Future Growth & Disruption Scenarios", "Industry - Core Economics & Market Structure", "Industry - Business Models & Ecosystem Architecture", "Industry - Value Chain Mapping & Key Players", "Industry - Unit Economics & Operating Leverage", "Industry - Geopolitics, Regulation & TAM"]
concept_agents = ["Concept - Investment Education & Metric Breakdown"]
ceo_agents = ["CEO - Track Record & Capital Allocation"]
stock_base_agents = [k for k in gem_prompts.keys() if k not in dependent_agents + industry_agents + concept_agents + ceo_agents]

# --- 4. UI SETUP ---
st.title("📈 B.E Research Investing Assistant")
st.markdown("**Professional stock and industry research, simplified for every investor.**")
st.warning("⚖️ **Legal Disclaimer:** Reports are for educational purposes only and are not financial advice. B.E Research is not a registered investment advisor.")

with st.sidebar:
    st.header("🔐 Admin Dashboard")
    auth_pass = st.text_input("Admin Password", type="password")
    if auth_pass == st.secrets.get("ADMIN_PASSWORD", ""):
        try:
            conn = sqlite3.connect('users.db')
            df = pd.read_sql_query("SELECT * FROM leads ORDER BY id DESC", conn)
            st.dataframe(df, use_container_width=True)
            conn.close()
        except Exception: pass

st.markdown("### Step 1: Target Information")
user_email = st.text_input("📧 Enter your email to receive the final report ZIP:")
user_email_clean = user_email.strip().lower()

is_super_user = user_email_clean in SUPER_USERS
if user_email_clean and "@" in user_email_clean and not is_super_user:
    p_runs, _, s_reps = get_usage(user_email_clean)
    st.markdown(f"⏳ **Remaining Quota:** {max(0, 4-p_runs)} Premium Runs | {max(0, 30-s_reps)} Standard Reports")

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
    brain_options = {"Fast Reasoning Engine (Standard)": "gemini-3.1-flash-lite-preview", "High Reasoning Engine (Advanced)": "gemini-3.1-pro-preview"}
    selected_brain_label = st.radio("🧠 Engine Power:", list(brain_options.keys()), index=0)
    selected_brain = brain_options[selected_brain_label]
with cfg_col2:
    tool_choice = st.radio("🔎 Search Method:", ["Standard Search", "Deep Research", "Market Data"], index=0)

st.markdown("---")
st.markdown("### Step 3: Select Reports")
st.info("💡 You can select multiple reports at once for a comprehensive analysis.")
selected_prompts = st.multiselect("📑 Choose research reports to generate:", list(gem_prompts.keys()), default=[])

# --- 5. BACKGROUND WORKER ---
def execute_background_job(email, ticker, company, industry, ceo, concept, prompts_to_run, brain_id, tool_id, api_key, email_sender, email_pwd, is_premium_run):
    client = genai.Client(api_key=api_key)
    reports = {}
    
    # Auto-resolve ticker if missing
    resolved_ticker = ticker.strip().upper()
    resolved_company = company.strip()
    if resolved_company and not resolved_ticker:
        try:
            res = client.models.generate_content(model='gemini-3.1-flash-lite-preview', contents=f"Ticker for {resolved_company}? Symbol only.")
            resolved_ticker = res.text.strip().upper()
        except: pass

    def fire_agent(agent_name, raw_instruction, extra_context=""):
        instruction = raw_instruction.replace("[STOCK NAME]", resolved_company).replace("[TICKER]", resolved_ticker).replace("[INSERT INDUSTRY]", industry).replace("[Industry Name]", industry).replace("{{CEO Name}}", ceo).replace("{{Company Name}}", resolved_company).replace("{CONCEPT NAME}", concept).replace("[Company_name]", resolved_company).replace("{Company_Name}", resolved_company).replace("[Company Name]", resolved_company).replace("[Insert stock]", resolved_ticker)
        instruction += "\n\nCRITICAL: Be exhaustive (1500+ words). List all 'SOURCES & REFERENCES' at the bottom. This is part of a B.E Research package."
        try:
            if extra_context and agent_name in dependent_agents:
                prompt = f"B.E RESEARCH SYNTHESIS:\n{instruction}\n\nDATA:\n{extra_context}"
                res = client.models.generate_content(model='gemini-3.1-pro-preview', contents=prompt)
                return agent_name, res.text
            if tool_id == "Deep Research":
                interaction = client.interactions.create(agent='deep-research-pro-preview-12-2025', input=instruction, background=True)
                while True:
                    interaction = client.interactions.get(interaction.id)
                    if interaction.status == "completed": return agent_name, interaction.outputs[-1].text
                    time.sleep(10)
            else:
                res = client.models.generate_content(model=brain_id, contents=instruction, config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]) if tool_id == "Standard Search" else None)
                return agent_name, res.text
        except Exception as e: return agent_name, f"Error: {e}"

    # Execution flow
    base_to_run = set(prompts_to_run)
    if any(p in dependent_agents for p in prompts_to_run): base_to_run.update(stock_base_agents)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fire_agent, n, gem_prompts[n]): n for n in base_to_run if n not in dependent_agents}
        for f in concurrent.futures.as_completed(futures):
            n, t = f.result()
            reports[n] = t

    dep_to_run = [p for p in prompts_to_run if p in dependent_agents]
    if dep_to_run:
        ctx = "\n\n".join([f"==={k}===\n{v}" for k,v in reports.items() if k in stock_base_agents])
        for n in dep_to_run:
            _, t = fire_agent(n, gem_prompts[n], ctx)
            reports[n] = t

    final_reports = {k: v for k, v in reports.items() if k in prompts_to_run}
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        for n, t in final_reports.items():
            safe_name = n.replace(" ", "_")
            zf.writestr(f"{resolved_ticker}_{safe_name}.doc", f"<html><body>{markdown.markdown(t)}</body></html>".encode('utf-8'))
    
    try:
        msg = MIMEMultipart()
        msg['From'] = f"B.E Research <{email_sender}>"
        msg['To'] = email
        msg['Subject'] = f"🚀 Research Complete: {resolved_company}"
        body = f"Your B.E Research package for {resolved_company} is attached.\n\nDISCLAIMER: Educational purposes only."
        msg.attach(MIMEText(body, 'plain'))
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(zip_buffer.getvalue()); encoders.encode_base64(part)
        part.add_header('Content-Disposition', f"attachment; filename={resolved_ticker}_B_E_Research.zip")
        msg.attach(part)
        server = smtplib.SMTP("smtp.gmail.com", 587); server.starttls()
        server.login(email_sender, email_pwd); server.send_message(msg); server.quit()
    except: pass
    global_tasks[email]["status"] = "complete"
    global_tasks[email]["reports"] = final_reports
    global_tasks[email]["zip"] = zip_buffer.getvalue()

# --- 8. GENERATE BUTTON ---
if st.button("🚀 Generate B.E Research Package", use_container_width=True):
    if not user_email or "@" not in user_email: st.error("Email required.")
    elif not selected_prompts: st.error("Select at least one report.")
    else:
        is_p = (selected_brain == "gemini-3.1-pro-preview" or tool_choice == "Deep Research")
        if not is_super_user:
            p_r, _, s_r = get_usage(user_email_clean)
            if is_p and p_r >= 4: st.error("Premium limit reached."); st.stop()
            if not is_p and s_r + len(selected_prompts) > 30: st.error("Standard limit reached."); st.stop()
        
        log_usage(user_email_clean, is_p, len(selected_prompts))
        save_lead(user_email_clean, target_ticker)
        global_tasks[user_email_clean] = {"status": "running", "ticker": target_ticker, "count": len(selected_prompts)}
        background_executor.submit(execute_background_job, user_email_clean, target_ticker, target_company, target_industry, target_ceo, target_concept, selected_prompts, selected_brain, tool_choice, st.secrets["GOOGLE_API_KEY"], st.secrets["EMAIL_SENDER"], st.secrets["EMAIL_PASSWORD"], is_p)

# --- 9. POLLING & DOWNLOAD ---
if user_email_clean in global_tasks:
    task = global_tasks[user_email_clean]
    if task["status"] == "running":
        est = (task["count"] * 35) / 60
        st.info(f"⏳ **B.E Research Engine is working...** Est. time: ~{est:.1f} min.")
        bar = st.progress(0)
        for i in range(100):
            time.sleep(est * 0.15)
            bar.progress(i + 1)
        st.rerun()
    elif task["status"] == "complete":
        st.success("✅ Research complete! Files emailed and available below.")
        st.download_button(label="📥 Download Research ZIP", data=task["zip"], file_name=f"{task['ticker']}_BEResearch.zip", mime="application/zip", use_container_width=True)
        for n, t in task["reports"].items():
            with st.expander(f"View: {n}"): st.markdown(t)

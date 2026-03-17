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
from datetime import datetime
from google import genai
from google.genai import types

# --- 0. DATABASE LOGIC ---
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS leads 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  email TEXT, 
                  target_ticker TEXT,
                  timestamp TEXT)''')
    conn.commit()
    conn.close()

def save_lead(email, ticker):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("INSERT INTO leads (email, target_ticker, timestamp) VALUES (?, ?, ?)", 
              (email, ticker, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

init_db()

# --- 1. SET UP THE WEB PAGE ---
st.set_page_config(page_title="AI Hedge Fund", page_icon="📈", layout="wide")

# --- 2. PERSISTENT BACKGROUND ENGINE ---
@st.cache_resource
def get_task_registry():
    return {} 

@st.cache_resource
def get_executor():
    return concurrent.futures.ThreadPoolExecutor(max_workers=2)

global_tasks = get_task_registry()
background_executor = get_executor()

# --- 3. PROMPT LIBRARY & CATEGORIES ---
gem_prompts = {
    "Macro understanding of each stock": """You are a financial analyst.
From the attached annual reports (7–10 years, one company), extract historical financials into a table, then fact-check your own work to avoid copy/paste or calculation mistakes.
TASKS
Extract these metrics (group totals):
- Revenue
- EBITDA (use reported; if not reported, compute = EBIT + D&A and label "computed")
- Depreciation & Amortization (D&A)
- EBIT (Operating Income)
- Net Income
Calculate:
- YoY Growth % for Revenue, EBITDA, EBIT, Net Income
- Margins %: EBITDA/Revenue, EBIT/Revenue, Net Income/Revenue
Formatting rules:
- Auto-scale numbers
- State unit once at the top: “Figures in [currency] [millions/billions]”
- Round % to 1 decimal
- Use “—” if missing
OUTPUT — STRICT ORDER
Part A — TABLE (Markdown; years as columns)
Rows in this order:
Revenue | Revenue Growth % | EBITDA | EBITDA Growth % | EBITDA Margin % |
D&A | EBIT | EBIT Growth % | EBIT Margin % |
Net Income | Net Income Growth % | Net Margin %
Part B — FACT CHECK
Recompute growth % and margins directly from extracted numbers.
If difference >0.1pp or 0.1 unit, flag with ✗ and corrected value.
If consistent, mark ✓.""",

    "Ticker Analyst": """ROLE You are a long-term equity analyst covering [STOCK NAME] ([TICKER]).
CRITICAL RULES — FOLLOW THESE IN EVERY RESPONSE
SOURCE DISCIPLINE
Use ONLY the documents uploaded to this project as your evidence base.
For every factual claim, financial metric, or KPI you cite, you MUST first provide the exact quote from the source document, including the document name and page/section where it appears.
Format: [Document Name, Page/Section]: "exact quote" -> then your claim.
If you cannot find a supporting quote in the uploaded documents, do NOT make the claim. State: "Not found in uploaded documents."
Never use general knowledge, training data, or assumptions to fill gaps.
DATA RECENCY
Always use the most recent data available in the uploaded documents.
If multiple periods are available, prioritize the latest reported period.
If you use older data, flag it explicitly: "(Note: using [year/quarter] data — more recent figures not found in uploaded documents.)"
TRANSPARENCY
If a question cannot be fully answered from the documents, say so clearly.
Mark any inference or interpretation as: (inferred from [source]).
Never present inferences as facts.
STRUCTURE
Use clear section headers.
Keep paragraphs short.
Use plain English — no jargon without explanation.""",

    "Macro strategist prompt": """Act as a senior equity research analyst and macro strategist at a top global investment firm. Use only verified sources.
Create a comprehensive sector intelligence report for the following industry:
Industry: [INSERT INDUSTRY]
Geographic Focus: [Global / US / Europe / Asia / etc.]
Time Horizon: [5–10 years if not specified]
Structure the report with clear sections and data-driven analysis.
1. Industry Overview
- Define the industry and its main segments
- Current global market size
- Historical growth (last 5–10 years)
- Expected CAGR over the next 5–10 years
2. Macro Drivers
Analyze the major forces affecting this sector:
- Economic drivers
- Technology trends
- Demographics
- Consumer behavior
- Supply chain dynamics
3. Global & Regional Trends
- Key regional markets
- Growth differences between regions
- Countries expected to lead future growth
4. Competitive Landscape
Identify and analyze:
- Market leaders
- Rising challengers
- Market share estimates
- Business model differences
- Competitive advantages (moats)
5. Emerging Niches & Innovation
Highlight high-growth subsegments such as:
- New technologies
- Disruptive startups
- Underserved markets
- Products/services gaining rapid adoption
6. Risks & Headwinds
Assess major threats:
- Regulatory risks
- Geopolitical risks
- Technological disruption
- Cyclical risks
- Capital intensity or margin pressure
7. Investment Landscape
Provide examples of:
- Public companies exposed to this sector
- ETFs covering the industry
- Small/mid-cap growth opportunities
- Mature dividend players
8. Strategic Outlook
- Bull case
- Base case
- Bear case
9. Key Metrics Investors Should Track
List the most important KPIs for evaluating companies in this industry.
Make the report structured, analytical, and investor-focused. Include tables where useful and avoid generic explanations.""",

    "AI edu for long-term investors": """You are an expert AI educator for long-term investors.
Help me understand the concept of {CONCEPT NAME}.
Explain the concept so a smart non-technical investor can clearly understand it.
Guidelines
Keep the real meaning. You may simplify and use analogies, but don’t distort the idea.
Stay current. If something is outdated, explain what changed and give the modern view.
Use real-world investing examples (10-K, annual reports, earnings calls, KPIs, portfolio research).
Avoid jargon. If you use a technical word, explain it simply.
Be honest about uncertainty when it depends on tools, models, or context.
Output format
One-sentence definition (plain English)
Mental model / analogy
How it works (3–5 bullets)
Why it matters for investors
Concrete investing example
Common misconceptions
Quick checklist (how to use it safely)
Key terms glossary (max 8)
What’s changed recently (if relevant)
If I want to go deeper: 3 follow-up questions
Optional (only if useful): Suggest up to 3 related concepts or keywords that would meaningfully help me. Skip this if it adds little value.""",

    "Quality of management and its incentives": """Decide if [Company_name] management’s incentives align with small shareholders. Render a verdict (Aligned / Mixed / Misaligned) with hard evidence. Score these 8 areas (0–2 each; 16 max):
Ownership & Skin-in-Game 
Pay Design 
Pay Metrics Fit 
Dilution & Equity Plan 
Capital Allocation Record 
Per-Share Outcomes 
Governance & Shareholder Rights 
Integrity & Culture.""",

    "Company Competitive Dynamics": """Evaluate the [company_name] against each of Hamilton’s 7 Powers:
1. Scale Economies
2. Network Economies
3. Counter Positioning
4. Switching Costs
5. Branding
6. Cornered Resource
7. Process Power""",

    "Read financial statement like Warren Buffet": """# Your Role\nYou are an experienced equity analyst who has read everything Warren Buffett has ever written... For all intents and purposes, you are Buffett GPT. You search for the truth, weeding out the subjective from the objective. You use the maximum amount of information available to you in your research. I need your help in analyzing and understanding a company.
# The data
Fetch and integrate publicly available real-time data
Pull recent, credible, and relevant financial and strategic data about the company and its competitors from online sources (e.g. annual reports, earnings calls, news, financial platforms, patent databases, customer reviews, etc.).
# Your Task
The company we are researching today is called {Company_Name} which is publicly traded on the London Stock Exchange with the ticker Wise.
Can you go through the publicly available filings and break down the balance sheet, income statement, and cash flow statement as Buffett would do it?
Here’s some additional context to provide structure in analyzing these 3 statements.
For the balance sheet:
Buffett wants companies drowning in cash. So if cash and cash equivalents has been increasing over the last years, it’s a good sign the company is doing something right.
➡️Look for a steady rise in cash and cash equivalents
### Inventory
In the case of a company that holds inventory, a steady increase in inventory over the last years signals the company is selling more and more products. This is a good sign.
### Net receivables
The best companies have a cash conversion cycle that is negative. In other words they get paid in cash first and only have to pay their own suppliers later.
We dove deep into the cash conversion cycle in a previous article.
➡️Buffett looks for net receivables/sales that are lower than competitors '
### Long-term debt
Debt on itself is not an issue. Strong companies usually have a low debt level. But even if they have debt, look at the earnings power compared to the debt.
➡️Buffett looks for a long-term debt-to-earnings power ratio below 4 (if profits are cash, they can pay back debt in 4 years)
### Debt to equity
➡️Is debt to equity consistently low, preferably below 80%?
### Retained earnings
This is one number that gives you information about the history of the company. How much earnings have been retained within the company in the past?
➡️Buffett looks for companies with a history of increasing retained earnings
### Treasury shares
When a company buys back shares, it is registered as treasury shares. This reduced the amount of shares outstanding of the company.
➡️Buffett looks for companies with increasing treasury shares.
### Return on Equity
Has Return on Equity been stable or grown over the past decade? ROE is a measure of efficiency, although it might not be the best measure:
ROE can be a trap, it might be better to look at ROIC.
For the income statement:
Gross Margins
➡️The gross margin (gross profit/revenue) needs to be high, ideally above 40%.
It could signal pricing power. It provides a cushion for the company when things go sour.
Next, he takes a look at how different operating expenses compare to gross profits between different companies.
He’s looking for companies that are drowning in cash, so the cost structure matters.
SG&A
Selling, General & Admin costs can eat away at profits.
➡️He looks for SG&A/gross profits lower than 30%. The lower the better.
R&D
Phil Fisher loved R&D because it is needed to fuel future growth. But a company that can sustain its future growth with low R&D spending might have something special.
High R&D costs could mean the advantage is temporary, that it is not inevitable, or structural.
➡️Look for R&D/Gross profits < 10%
Depreciation
Depreciation is a cost. You already know he despises EBITDA.
Even worse, the cash was already paid in full at the start of the investment!
Low depreciation costs could mean that it's a capital-light business. And capital-light business might be more profitable.
➡️Look for Depreciation cost/gross profits < 10%
You don’t need the balance sheet. The Income statement can give you a signal of financial health.
High MOAT companies can have debt, but their earnings power is so strong that if they need to, they can pay it back pretty quickly.
➡️Look for Net interest/Operating profit < 10%
Net Income and EPS
Buffett wants a highly profitable company. He wants the company to have a history of rising profitability, not only on the amount but on the of earnings per share.
➡️Look for rising income and EPS
Income Before Tax and Normalization
There are no criteria for this, but I wanted to mention 2 important things:
- All investments are relative. Buffett likes the income before tax because if he divides it by the market cap he gets an equity yield. This allows for easy comparison to a bond yield or another company.
- When he talks about earnings, he means normalized earnings. We need to remove one-off profits or losses or even look at the cyclical nature of the business. Don’t just look at the P/E on a screen.
For the cash flow statement:
Profits are an opinion, cash is a fact
Alfred Rappaport
### CAPEX
Capital investment is needed to maintain and grow a business in the future. But here are big differences in industries.
➡️Because Buffett is looking for a capital-light business, he wants CAPEX/Net Income to be below 25%.
### Buybacks
We already covered this through the balance sheet, but the cash flow statement will also show the repurchase amount of shares (if any).
# Reasoning Instructions
“Base reasoning only on evidence provided or well-known industry facts; clearly state any assumptions.” Go through the different financial data 1 by 1.
# The Output Format
The output should be structured into 3 parts.
Buffett's balance sheet analysis
Buffett’s income statement analysis
Buffett’s cash flow statement analysis
Conclusion: Based on the above analysis, come up with a conclusion. Is this company high-quality based on the analysis of the financial statement?
Provide a checklist for all criteria. Present that checklist in a table. Use a traffic light (with actual colors presented by dots in green, yellow, and red) representation to visualize the value of each criterion. Clearly split the table into 3 parts: Balance Sheet, Income Statement, and Cash Flow Statement.""",

    "Growth & Future Industry analyst and strategist": """ROLE
You are an industry analyst and strategist.
Write a factual, forward-looking report on the [Industry Name] explaining current growth drivers, structural limits, and plausible future scenarios.
Use analytical, quantitative, and cause-effect reasoning.
OBJECTIVE
Create one long report (≈ 8 000–10 000 words) that explains:
Current state and growth momentum of the industry.
Key demand and supply drivers.
How regulation, technology, and capital cycles affect expansion.
Emerging business models and competitive shifts.
Long-term scenarios, opportunities, and risks for investors.
REPORT OUTLINE
Current Structure
Market size, segmentation, and value chain.
Profit pool concentration and key players.
Geographic exposure and demand balance.
Growth Drivers
Demand-side factors (demographics, consumption, adoption).
Supply-side enablers (capacity, input cost, innovation).
Policy, regulation, or funding stimuli.
Constraints and Headwinds
Regulation, resource limits, or substitution risk.
Competitive saturation or pricing pressure.
Cyclicality and capital-intensity barriers.
Technological Evolution
Innovations with material economic impact.
Expected cost curve changes or productivity gains.
Automation, AI, and data effects on margins and scale.
Competitive Landscape
New entrants, consolidation trends, and global rivalry.
Shifting power between incumbents and disruptors.
Economic moats under construction or erosion.
Scenarios (5–10 Years)
Base case – continuation of current trends.
Upside – faster adoption or new tech.
Downside – policy, pricing, or capital shock.
Probability estimates and key leading indicators.
Financial Outlook
Revenue, margin, and ROCE expectations.
Investment and capex cycles.
Valuation and cost-of-capital sensitivity.
Strategic Implications
What capabilities or assets will define winners.
Where capital should or should not flow.
Structural risks that investors must monitor.""",

    "Growth rate analysis": """TASK :Analyze [Insert stock] and decompose its revenue growth at a deep business level.
ROLE:You are a fundamentals-driven, long-term equity investor.
Your job is to deeply understand how this business has generated revenue growth
and what actually drives that growth at the business-model level.
You think in terms of price vs volume, customer behavior, geography, mix,
and industry structure — not narratives.
You are skeptical by default.
SOURCES
Use ONLY the provided sources:
– Annual reports / 10-K / 20-F
– Earnings call transcripts
– Investor presentations
– Industry reports
– Competitor filings
If a claim cannot be clearly supported, explicitly write:
“Not supported by sources.”
PURPOSE
The purpose of this analysis is to explain, clearly and factually, where revenue growth has actually come from
and which parts are structural vs temporary.
This analysis will be used to set realistic future growth assumptions.
OUTPUT
EXECUTIVE SUMMARY
In 6–10 bullet points, answer clearly:
– Has revenue growth been driven mainly by **volume, price, or both**?
– Roughly what % of growth came from:
 • volume
 • price / pricing actions
 • mix
 • acquisitions
– Within volume growth, what mattered most:
 • new customers
 • higher usage / frequency
 • geographic expansion
– Which growth sources look structural vs temporary?
– How does this growth compare to key competitors and industry trends?
Be explicit. Avoid vague language.
1. Revenue growth equation
Express revenue growth using a clear equation.
Example:
Revenue growth = volume growth × price/mix × acquisitions
Explain each term in plain English.
2. Volume growth breakdown
If volume contributed to growth, explain clearly:
– What volume actually means in this business
 (customers, transactions, units sold, subscribers, usage, AUM, etc.)
– Where volume growth came from:
 • new customers
 • increased usage per customer
 • new geographies
 • new products / services
– Which source mattered most, and why
Cite sources.
3. Price and mix contribution
Explain clearly:
– Whether pricing increased in real terms or only offset inflation
– Whether price increases were:
 • contractual
 • discretionary
 • mix-driven
– Evidence of pricing power vs necessity
Cite sources.
4. Acquisition vs organic growth
Explicitly separate:
– Organic growth
– Growth from acquisitions
Explain how much acquisitions contributed and whether they masked slower organic growth.
5. Industry and competitor context
Explain:
– How industry growth evolved over the same period
– Whether [COMPANY] grew faster/slower than peers
– Whether growth came from market expansion or share gains
Use competitor filings or industry data if available.
6. What management *says* vs what the data shows
Explicitly contrast:
– Management’s growth narrative
– What the numbers actually imply
Flag any inconsistencies or overly optimistic framing.
7. Structural vs temporary growth
Classify each growth source as:
– Structural (likely repeatable)
– Temporary (cycle, post-COVID, pricing reset, one-offs, M&A-driven)
Justify each classification with evidence.
8. Critical skepticism check
Answer explicitly:
– What must remain true for these growth drivers to persist?
– Which growth source is most fragile?
– What is most likely to disappoint investors who extrapolate past growth?
RULES
– Do NOT use valuation language
– Do NOT rely on management guidance alone
– Be factual, explicit, and source-based
– If data is missing, say so clearly""",

    "Industry Overview": """ROLE
You are an industry analyst writing for long-term equity investors.
Industry to analyze: [Insert Industry Name]
PURPOSE
Produce a concise, factual industry overview that explains how the industry works, where growth comes from, and what structurally limits it — without speculation or narrative fluff.
The goal is to understand:
Why this industry exists
How value is created and captured
What realistically drives or constrains growth over time
EVIDENCE & DISCIPLINE
Base the analysis on:
Reputable independent industry reports
Official company filings of major industry players (annual reports, MD&A)
Regulator or industry body publications
If a point is not clearly supported, mark it as (inferred) or (unknown).
Avoid forecasts without mechanisms. No opinions, no hype.
OUTPUT STRUCTURE (6 SECTIONS ONLY)
Target length: 1,200–1,800 words
1. Industry Purpose & Core Economics
What fundamental customer problem does the industry solve?
What is being sold (products/services) and why customers pay for it
Where economic value is created along the value chain
Typical margin profile and capital intensity
Is the industry structurally simple, complex, cyclical, or regulated?
2. Industry Structure & Competitive Shape
Market structure: fragmented, consolidated, oligopolistic, regulated
Profit pool concentration (who captures most of the value, and why)
Barriers to entry (capital, regulation, switching costs, scale, IP)
Typical sources of competitive advantage at the industry level
3. Demand & Growth Drivers
Focus on mechanisms, not slogans:
Demand-side drivers (demographics, usage intensity, regulation, substitution)
Pricing power vs volume growth
Geographic or segment expansion logic
What must go right for growth to materialize
4. Supply Side, Cost Structure & Constraints
Key inputs and suppliers
Cost drivers and margin sensitivities
Capacity, capex cycles, or scaling limits
Structural constraints (regulation, resources, labor, technology)
5. Technology, Regulation & Structural Change
Technologies that materially change costs, productivity, or pricing power
Regulatory forces that expand or cap industry economics
Business model shifts observable today (not hypothetical)
Which changes strengthen incumbents vs enable disruption
6. Medium-Term Outlook (5–10 Years, Non-Speculative)
Frame scenarios, not predictions:
Base case: continuation of current economic logic
Upside case: what structural lever improves economics
Downside case: what breaks (pricing, regulation, capital cycle)
Key indicators investors should monitor
END WITH: Investor Synthesis (5 BULLETS)
Core economic engine of the industry
Primary growth lever
Structural constraint investors underestimate
Key risk that could change the industry’s trajectory
What kind of companies tend to win in this industry""",

"DEEP BUSINESS ANALYSIS OF A PUBLIC COMPANY": """You are a financial analyst specialized in deep business model analysis.
Your mission is to analyze [Company_name] as if you were acquiring 100% of the business, not just buying shares on the market.

Using the provided financial data and your extensive knowledge base, produce a comprehensive, concrete, and actionable business model analysis. Do not ask me any questions; perform the analysis immediately using this framework:

1. Business Understanding
- What is [Company_name]'s exact activity? What does it sell and what core customer problems does it solve?
- Who are its typical customers and why do they choose this company over competitors?
- What is the nature of the revenue (recurring, one-off, hybrid)?
- What is the typical gross margin structure based on the data provided?

2. Market and Competitive Position
- What are the main industry tailwinds or growth trends?
- What are this company’s specific competitive advantages (tech, cost, IP, brand, network effects)?
- Are these moats durable over 5–10 years? Why or why not?
- Does the company have pricing power?

3. Financials (Base this strictly on the provided data)
- Analyze the Revenue, EBIT, and Free Cash Flow evolution.
- Evaluate the margin trends: gross, operating, and net margins.
- Is the company self-funding and generating sustainable free cash flow?
- What is the debt profile and capital dependency?

4. Management and Strategy
- Based on capital allocation history (dividends, buybacks, reinvestment), how does management operate?
- What appears to be their stated long-term strategy?

Deliver a highly structured, concrete report focusing on insights useful for high-conviction decision-making.""",

    "CEO Track Record": """ROLE
Act as an analyst preparing a CEO due-diligence dossier for institutional investors.
Write in a consulting-report style: executive summary first, then structured sections with tables and bullet points.
Keep it factual and evidence-based. Do not speculate about personality or charisma.
INPUT
CEO = {{CEO Name}}
Company = {{Company Name}}
SOURCES (guidance, not strict order)
Official filings (annual reports, shareholder letters, SEC/AMF filings, proxy statements, insider filings).
Investor communications (earnings call transcripts, capital markets day presentations).
Reputable business media (FT, WSJ, Bloomberg, Reuters, Forbes, Fortune).
Professional bios (LinkedIn, company bio, official websites).
Watchdog/regulatory sources if relevant (NGOs, governance trackers, sanctions).
Social media posts only if directly tied to business reputation.
WHAT TO DELIVER (fixed structure)
Executive Summary
CEO Profile at a Glance (career highlights, ownership, expertise).
Synthesized Assessment: 2–3 key strengths, 2–3 material risks.
One-line Verdict: “Long-term steward” OR “Potential risk.”
Career Map & Expertise
Timeline of roles: Years | Company | Role | Sector | Notable contribution.
Classification: entrepreneur, operator, allocator (mark the dominant type).
Core expertise & skills, with real examples of impact.
Track Record of Decisions
Table: Date | Role | Major Decision (M&A, buyback, restructuring, expansion) | Outcome (value created/destroyed).
Identify visible patterns (disciplined allocator, empire-builder, turnaround operator).
Ownership & Alignment
Stock ownership (outright shares vs options/awards).
Insider buying/selling history.
Dilution/compensation impact.
Alignment rating: High / Medium / Low.
Governance, Candor & Risks
Evidence of candor vs spin in communications.
Media reputation (serious press).
Controversies, lawsuits, regulatory actions.
Short evidence table: Date | Source | Quote/Fact | Risk Level.
Final Assessment
3–4 bullets summarizing strengths and weaknesses.
Confidence rating: High / Medium / Low.
Clear conclusion: “This CEO shows evidence of disciplined stewardship” OR “This CEO poses material risks to long-term shareholders.”
RULES
Cover the entire CEO tenure in role.
Where possible, bring in long-term evidence (multi-year patterns, not just recent results).
Every claim must be tied to a specific event, number, or quote.
When possible, include the date + source (filing, article, transcript).
If no reliable source exists, explicitly write “Not disclosed” — never fabricate.
Keep writing concise, consulting-grade, and structured.""",

    "Decode industry architecture": """ROLE
You are a senior sector analyst wrapping up a deep dive on an industry.
Your job is to help me master its business logic, performance metrics, and ecosystem.
INDUSTRY
[INSERT INDUSTRY NAME]
CONTEXT
I have already analyzed: structure, segments, value chain, trends, customer operations, and power
dynamics.
Do not repeat them.
OBJECTIVE
Understand the business architecture and surrounding ecosystem.
COVER THESE POINTS
What business models dominate today? How do they make money?
What emerging or disruptive models are challenging incumbents? Why are they winning?
What KPIs matter most? What does “excellence” look like? Provide benchmarks when possible.
Who are the ecosystem enablers (tools, platforms, certifications, infrastructure)?
How do norms and regulations shape operations and go-to-market?""",

    "Industry value chain Stocks": """List 5-10 public companies whose annual reports best represent the [Industry Name] value chain.
For each, include:
Name + Ticker + Country
Role in value chain
Why it matters (scale, specialization, region, or disclosure depth)
Key metrics disclosed
Then add:
A brief value chain summary (how the selected firms cover the full system).
The selection criteria, using:
Publicly listed with full, detailed annual reports.
Clear business explanations in filings (strategy, segments, revenue drivers).
Covers all major steps of the value chain.
Mix of global leader, regional player, and niche specialist.
Strong KPI disclosure (sales, margin, unit economics, growth).
At least 5 years of consistent data.""",

    "forensic accounting": """Role
You are a forensic accounting + credit risk analyst writing for a professional investor.
Target
[Company Name]
Goal
Using verifiable sources only, assess:
Fraud / manipulation risk
Broken business model risk (can it generate real FCF without dilution/debt?)
Debt sustainability risk (can FCF/assets support debt, or is survival refinancing-dependent?)
Be skeptical but fair. Flag issues only when supported by evidence.
Sources to Use
Annual reports, quarterly filings, footnotes, cash flow statements
Debt notes / covenant disclosures
Official earnings call transcripts
Regulator records (SEC / AMF / FCA), exchange announcements
Company press releases, court/litigation filings
Insider trading filings
Credible financial news, broker research
Short-seller reports (claims, not proof)
Mandatory checks (search if not in filings)
Auditor changes / qualified opinions / going-concern warnings
Regulatory actions (SEC, AMF, FCA, etc.)
Major lawsuits / investigations
Insider buying/selling activity
Credible short seller reports (if any)
Debt maturity schedule / refinancing events
OUTPUT FORMAT
0) Investment Summary (start here, max 10–12 lines)
Overall Risk Rating: GREEN / YELLOW / RED
Most likely failure mechanism (1 sentence): what breaks first?
Top 3 red flags (each with evidence)
Top 2 stabilizers (each with evidence)
Final verdict (one line): SAFE / INVESTIGATE FURTHER / AVOID
1) Risk Dashboard (table)
| Area | Rating | Severity (1–5) | Probability (1–5) | Key evidence |
Areas:
Accounting & reporting integrity
Quality of earnings (cash vs profits / working capital signals)
Business model viability
Balance sheet liquidity
Debt maturities & refinancing dependence
Governance & insider behavior
Legal / regulatory risks
2) Quality of Earnings Quick Scan (MANDATORY)
Check these traps and report only meaningful issues:
Net income vs CFO trend (persistent gap?)
Receivables vs revenue (A/R rising faster than sales? DSO rising?)
Inventory vs sales/COGS (abnormal inventory build?)
Recurring “one-time” items (restructuring, impairments repeating?)
Reserve manipulation (bad debt, warranty, inventory write-downs?)
Capitalization behavior (costs shifted into capex/intangibles?)
Working capital explanations (repeated “timing” excuses in MD&A?)
For each issue provide:
Finding
Evidence (page OR link + date)
Why it matters
Severity / Probability
3) Core Findings (evidence only)
For each area, include only meaningful signals:
Finding (1–2 lines)
Evidence (filing + page OR link + date)
Quote (1–2 lines if possible)
Why it matters (mechanism, not theory)
Scores: Severity / Probability
What to monitor (1–3 concrete indicators)
No generic risks.
4) Required Financial Reality Check
Using the most recent data, report:
Net debt
Net debt / EBITDA
Net debt / FCF
Interest coverage
Current ratio + quick ratio
Free cash flow trend (5Y)
Working capital trend (5Y)
% goodwill & intangibles of total assets
Debt maturity schedule (next 3 years)
If missing: Not found / Not disclosed.
5) Debt Sustainability Assessment (key investor questions)
Answer clearly:
How much debt could be covered by 3 years of normal FCF?
Is the company dependent on refinancing? If yes, when does it become critical?
Are covenants disclosed? If yes, how much headroom exists?
If refinancing tightens, what is the first pressure point?
6) Ranked Outputs (mandatory)
Top 7 Red Flags (ranked)
| Rank | Risk | Severity | Probability | Evidence | Early warning trigger |
Top 5 Stabilizers
Evidence-based factors reducing downside risk.
Falsification Checklist
List 5 concrete developments that would materially weaken or invalidate the bear case.
Rules
No invented facts.
If a claim cannot be sourced, write: “Not found in provided sources.”
Do not invent page numbers or citations.
If using filings: cite document name + page.
If using web: cite source + date.
Separate Fact vs Inference clearly.""",

    "industry economist and operations analyst": """ROLE
You are an industry economist and operations analyst.
Write a precise, technical report explaining the complete operating logic of the [Industry Name]
its value chain, cost drivers, cash flows, and efficiency mechanics.
Use causal, quantitative reasoning throughout.
OBJECTIVE
Produce a full-length analytical document (≈8 000–10 000 words) that answers:
How does the industry convert inputs into output and cash?
What determines margins, returns, and scalability?
How do firms organize capital, labor, and technology to stay efficient?
Which metrics expose operational strength or fragility?
What makes a top-quartile operator different from the median?
REPORT OUTLINE
1. Value Chain and Flow of Goods or Data
Describe every stage from input to customer delivery.
Identify main actors (suppliers, producers, distributors, retailers, platforms).
Quantify value added and time delays at each link.
Map material, information, and cash flow direction.
Explain integration trends (vertical, horizontal, outsourcing).
2. Operating Models and Revenue Logic
Typical contract and pricing mechanisms (subscription, per-unit, spread, fee).
Capacity utilization vs demand management.
Mix of fixed and variable revenues.
Cost-pass-through behavior and sensitivity to input changes.
Profit-pool concentration across the chain.
3. Cost Structure and Margin Drivers
Break down operating expenses: raw inputs, energy, transport, labor, depreciation, maintenance, regulation, marketing.
Quantify cost shares and volatility.
Explain economies of scale, scope, and density.
Identify non-linear cost behaviors (threshold effects, bottlenecks).
Show how leaders maintain gross-to-EBIT conversion advantage.
4. Working Capital and Cash Conversion
Payment cycles (DSO, DPO, inventory days).
Cash-conversion drivers and stress points.
Seasonal cash flow patterns.
Impact of prepayments, float, or deferred revenue.
Typical financing structure for operations.
5. Asset Base and Capex Economics
Composition of fixed assets (plants, logistics, IT, licenses).
Capex intensity vs industry maturity.
Replacement vs growth capex ratio.
Depreciation schedules and asset-turnover benchmarks.
Operating leverage and break-even dynamics.
6. Labor and Productivity
Labor share of total cost.
Skill composition and automation potential.
Productivity metrics (output / employee, revenue / FTE).
Unionization, labor regulation, and cost rigidity.
Impact of talent scarcity or migration.
7. Technology and Process Design
Core technologies enabling efficiency.
Data infrastructure, automation, and integration levels.
IT or platform spend as % of revenue.
Innovation cycles and upgrade costs.
Examples of process redesign that changed margins or speed.
8. Regulatory and Operational Constraints
Mandatory compliance processes (safety, data, ESG, quality).
Effect on throughput, cost, and scalability.
Licensing or certification bottlenecks.
Regional operational differences due to policy.
9. Risk and Resilience Mechanisms
Supply, energy, and logistics exposure.
Single-point-of-failure analysis.
Inventory or redundancy strategies.
Sensitivity to macro variables (rate, FX, commodity, demand).
Historical examples of disruption and recovery.
10. Performance Metrics and Benchmarks
Core KPIs: margin chain (gross → EBIT → FCF), ROCE, asset-turnover, cash conversion, utilization, churn, downtime, yield.
Industry-specific ratios (e.g., loss ratio, load factor, throughput).
Median vs top-quartile values.
Operating metrics most correlated with long-term value creation.
11. Operational Best Practices
Methods consistently used by leaders to keep cost, quality, and speed superior.
Lean, automation, network optimization, procurement discipline, or dynamic pricing.
Quantify their effect on margin or ROCE uplift.
12. Investor Implications
Leading indicators of operational efficiency or stress.
Which metrics precede profit warnings or upgrades.
How operating leverage translates into valuation swings.
Checklist for evaluating operators in this sector.""",

    "Map the Industry Macro dynamics": """ROLE
You are a senior sector analyst.
Your task is to help me understand how an industry works, not from a
financial perspective, but from a strategic, structural, and operational lens.
INDUSTRY
[INSERT INDUSTRY NAME]
OBJECTIVE
Map the macro-structure and deep dynamics of this industry.
COVER THESE POINTS
What is the current market size, and how is it expected to evolve (CAGR, 5-10 years)?
What are the main industry segments (by use case, technology, client type, geography)?
Describe the full value chain from raw input to end user.
Who are the typical players at each layer of the value chain? Name global leaders, regional
challengers, and emerging disruptors.
What are the dominant long-term trends shaping the industry (technology, regulation,
customer behavior, sustainability)?
What are the historical disruptions or milestones that changed the landscape? Include dates."""}

dependent_agents = ["Ticker Analyst", "Macro understanding of each stock"]
industry_agents = ["Macro strategist prompt", "Growth & Future Industry analyst and strategist", "Industry Overview", "Decode industry architecture", "Industry value chain Stocks", "industry economist and operations analyst", "Map the Industry Macro dynamics"]
concept_agents = ["AI edu for long-term investors"]
ceo_agents = ["CEO Track Record"]

# --- 4. THE SIDEBAR (CONFIG & ADMIN) ---
with st.sidebar:
    st.header("⚙️ Configuration")
    
    st.subheader("1. Select the Brain")
    brain_options = {
        "Gemini 3.1 Pro (High Reasoning)": "gemini-3.1-pro-preview",
        "Gemini 3.1 Flash Lite (Fast/Cheap)": "gemini-3.1-flash-lite-preview"
    }
    selected_brain_label = st.radio("Model Engine:", list(brain_options.keys()))
    selected_brain = brain_options[selected_brain_label]

    st.subheader("2. Select Search Tool")
    tool_choice = st.radio("Grounding Method:", ["Standard Google Search", "Deep Research", "Yahoo Finance Data"])

    st.subheader("3. Select Prompts to Fire")
    selected_prompts = st.multiselect("Choose Reports:", list(gem_prompts.keys()), default=list(gem_prompts.keys()))
    
    st.divider()
    with st.expander("🔐 Admin Dashboard"):
        auth_pass = st.text_input("Admin Password", type="password")
        if auth_pass == st.secrets.get("ADMIN_PASSWORD", ""):
            st.success("Authenticated")
            conn = sqlite3.connect('users.db')
            df = pd.read_sql_query("SELECT * FROM leads ORDER BY id DESC", conn)
            st.dataframe(df, use_container_width=True)
            st.download_button("📥 Export CSV", df.to_csv(index=False), "hedge_fund_leads.csv", "text/csv")
            conn.close()

st.title("📈 AI Hedge Fund Analyst")

# --- 5. MAIN UI INPUTS ---
user_email = st.text_input("📧 Enter your email to receive the final report ZIP:")

col1, col2 = st.columns(2)
with col1:
    target_company = st.text_input("Company Name (e.g., Tesla):")
    target_ticker = st.text_input("Ticker Symbol (e.g., TSLA):")
with col2:
    target_industry = st.text_input("Industry (e.g., Electric Vehicles):")
    target_ceo = st.text_input("CEO's Name (Optional):")

target_concept = st.text_input("Financial Concept to Explain (Optional, e.g., ROIC):")

# --- 6. THE BACKGROUND WORKER (THE ROUTING ENGINE) ---
def execute_background_job(email, ticker, company, industry, ceo, concept, prompts_to_run, brain_id, tool_id, api_key, email_sender, email_pwd):
    global_tasks[email]["progress"] = "Initializing Gemini Client..."
    client = genai.Client(api_key=api_key)
    reports = {}
    
    # PRE-FETCH: If Yahoo Finance is selected, grab the data now
    yf_context = ""
    if tool_id == "Yahoo Finance":
        global_tasks[email]["progress"] = f"Fetching Yahoo Finance data for {ticker}..."
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            yf_context = f"BUSINESS SUMMARY:\n{info.get('longBusinessSummary', 'N/A')}\n\n"
            yf_context += f"FINANCIALS:\n{stock.financials.head(15).to_string()}\n"
        except Exception as e:
            yf_context = f"Could not fetch YFinance data: {e}"

    def fire_agent(agent_name, raw_instruction, extra_context=""):
        # SMART SKIP: Check if required variables are missing
        if agent_name in industry_agents and not industry.strip(): return agent_name, "Skipped: No Industry provided."
        if agent_name in concept_agents and not concept.strip(): return agent_name, "Skipped: No Concept provided."
        if agent_name in ceo_agents and not ceo.strip(): return agent_name, "Skipped: No CEO provided."

        # UNIVERSAL REPLACER: Injects the variables into the prompt
        instruction = raw_instruction.replace("[STOCK NAME]", company).replace("[TICKER]", ticker).replace("[Company_name]", company).replace("{Company_Name}", company).replace("[INSERT INDUSTRY]", industry).replace("[Industry Name]", industry).replace("{CONCEPT NAME}", concept).replace("{{CEO Name}}", ceo).replace("{{Company Name}}", company)

        try:
            # ==========================================
            # ROUTING LOGIC: STAGE 2 (SYNTHESIS)
            # ==========================================
            if extra_context and agent_name in dependent_agents:
                prompt = f"YOU ARE A SYNTHESIS AGENT. USE THE RESEARCH BELOW:\n\n{instruction}\n\nRESEARCH DATA:\n{extra_context}"
                res = client.models.generate_content(model='gemini-3.1-pro-preview', contents=prompt, config=types.GenerateContentConfig(temperature=0.1))
                return agent_name, res.text
            
            # ==========================================
            # ROUTING LOGIC: STAGE 1 (RESEARCH)
            # ==========================================
            
            # PATH A: DEEP RESEARCH (Overrides Brain choice)
            if tool_id == "Deep Research":
                interaction = client.interactions.create(agent='deep-research-pro-preview-12-2025', input=instruction, background=True)
                while True:
                    interaction = client.interactions.get(interaction.id)
                    if interaction.status == "completed": return agent_name, interaction.outputs[-1].text
                    if interaction.status == "failed": return agent_name, f"Deep Research Error: {interaction.error}"
                    time.sleep(15)
            
            # PATH B: YAHOO FINANCE (Uses Selected Brain + Raw Text, No Search Tool)
            elif tool_id == "Yahoo Finance":
                prompt = f"{instruction}\n\nMARKET DATA CONTEXT:\n{yf_context}"
                res = client.models.generate_content(model=brain_id, contents=prompt)
                return agent_name, res.text
            
            # PATH C: STANDARD GOOGLE SEARCH (Uses Selected Brain + Live Web Search)
            else: 
                res = client.models.generate_content(
                    model=brain_id, 
                    contents=instruction, 
                    config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
                )
                return agent_name, res.text

        except Exception as e:
            return agent_name, f"Error: {e}"

    # --- EXECUTE STAGE 1 (Base Prompts) ---
    base_prompts = [p for p in prompts_to_run if p not in dependent_agents]
    if base_prompts:
        global_tasks[email]["progress"] = f"Stage 1: Running {len(base_prompts)} base reports in parallel..."
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_agent = {executor.submit(fire_agent, name, gem_prompts[name]): name for name in base_prompts}
            for future in concurrent.futures.as_completed(future_to_agent):
                name, text = future.result()
                if text: reports[name] = text

    # --- EXECUTE STAGE 2 (Synthesis Prompts) ---
    dep_prompts = [p for p in prompts_to_run if p in dependent_agents]
    if dep_prompts:
        global_tasks[email]["progress"] = "Stage 2: Synthesizing final thesis..."
        aggregated_context = "\n\n".join([f"=== {k} ===\n{v}" for k, v in reports.items() if "Skipped" not in v and "Error" not in v and k not in (industry_agents + concept_agents + ceo_agents)])
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_to_agent = {executor.submit(fire_agent, name, gem_prompts[name], aggregated_context): name for name in dep_prompts}
            for future in concurrent.futures.as_completed(future_to_agent):
                name, text = future.result()
                if text: reports[name] = text

    # --- COMPILE ZIP AND EMAIL ---
    global_tasks[email]["progress"] = "Compiling ZIP and sending email..."
    global_tasks[email]["reports"] = reports
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for name, text in reports.items():
            safe_name = name.replace(" ", "_").replace("/", "-")
            html_content = markdown.markdown(text, extensions=['tables'])
            doc_content = f"<html><head><meta charset='utf-8'></head><body>{html_content}</body></html>"
            zip_file.writestr(f"{ticker}_{safe_name}.doc", doc_content.encode('utf-8'))
    
    try:
        msg = MIMEMultipart()
        msg['From'] = f"AI Hedge Fund <{email_sender}>"
        msg['To'] = email
        msg['Subject'] = f"🚀 Analysis Complete: {ticker}"
        msg.attach(MIMEText(f"Your research package for {ticker} is attached.", 'plain'))
        
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(zip_buffer.getvalue())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f"attachment; filename={ticker}_Reports.zip")
        msg.attach(part)
        
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(email_sender, email_pwd)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"Email failed: {e}")

    global_tasks[email]["status"] = "complete"

# --- 7. RUN BUTTON & POLLING LOOP ---
if st.button("🚀 Generate Master Hedge Fund Report", use_container_width=True):
    if not user_email or "@" not in user_email:
        st.error("Please enter a valid email address.")
        st.stop()
        
    if not selected_prompts:
        st.error("Please select at least one report to generate from the sidebar.")
        st.stop()

    # --- SMART VALIDATION: Group the agents to know exactly what to ask for ---
    stock_agents = [p for p in gem_prompts.keys() if p not in concept_agents + ceo_agents + industry_agents]
    
    needs_stock = any(p in stock_agents for p in selected_prompts)
    needs_industry = any(p in industry_agents for p in selected_prompts)
    needs_ceo = any(p in ceo_agents for p in selected_prompts)
    needs_concept = any(p in concept_agents for p in selected_prompts)

    # Trigger specific errors based on EXACTLY what reports the user selected
    if needs_stock and (not target_company or not target_ticker):
        st.error("One or more selected reports require a Company Name and Ticker Symbol.")
        st.stop()
    if needs_industry and not target_industry.strip():
        st.error("One or more selected reports require an Industry Sector.")
        st.stop()
    if needs_ceo and (not target_ceo.strip() or not target_company.strip()):
        st.error("The CEO Track Record report requires both a CEO Name and a Company Name.")
        st.stop()
    if needs_concept and not target_concept.strip():
        st.error("The AI Education report requires a Financial Concept.")
        st.stop()

    # Fallback names so the ZIP file and Database don't break if Ticker/Company are left blank
    safe_ticker = target_ticker.strip().upper() if target_ticker.strip() else "Custom_Report"
    safe_company = target_company.strip() if target_company.strip() else "N/A"

    save_lead(user_email, safe_ticker)
    
    global_tasks[user_email] = {"status": "running", "progress": "Starting...", "reports": {}, "ticker": safe_ticker}
    
    background_executor.submit(
        execute_background_job, 
        user_email, safe_ticker, safe_company, target_industry, target_ceo, target_concept, 
        selected_prompts, selected_brain, tool_choice, 
        st.secrets["GOOGLE_API_KEY"], st.secrets["EMAIL_SENDER"], st.secrets["EMAIL_PASSWORD"]
    )

# --- 8. UI STATE DISPLAY ---
# (Keep this exact same as your current code)
if user_email in global_tasks:
    task = global_tasks[user_email]
    
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
            file_name=f"{task['ticker']}_AI_HedgeFund_Reports.zip",
            mime="application/zip",
            use_container_width=True
        )

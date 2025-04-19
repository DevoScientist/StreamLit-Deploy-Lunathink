import streamlit as st
import requests
import json
import re
from bs4 import BeautifulSoup
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain.schema import HumanMessage, AIMessage
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Literal, Annotated
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

PROMPT_DIR = "prompts"

# ✅ PromptLoader Class
class PromptLoader:
    def __init__(self, profile: str, base_dir: str = PROMPT_DIR):
        self.profile = profile
        self.base_dir = base_dir

    def load(self, prompt_name: str) -> str:
        path = f"{self.base_dir}/{self.profile}/{prompt_name}.md"
        with open(path, "r") as file:
            return file.read()

# ========== MODELS ==========
class ResultRelevance(BaseModel):
    explanation: str
    id: str

class RelevanceCheckOutput(BaseModel):
    relevant_results: List[ResultRelevance]

class State(TypedDict):
    messages: Annotated[list, add_messages]
    summaries: List[dict]
    approved: bool
    created_summaries: Annotated[List[dict], Field(description="The summaries that have been created")]

# ========== SEARCH ==========
def search_serper(search_query):
    url = "https://google.serper.dev/search"
    payload = json.dumps({"q": search_query, "gl": "gb", "num": 30, "tbs": "qdr:d"})
    headers = {
        "X-API-KEY": "58670c52d6dbd47c4c094dd01556874d28ea3e6e",
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, data=payload)
    results = response.json().get("organic", [])
    return [{
        "title": r["title"],
        "link": r["link"],
        "snippet": r["snippet"],
        "search_term": search_query,
        "id": idx + 1,
    } for idx, r in enumerate(results)]

def check_search_relevance(results, prompt_loader: PromptLoader):
    prompt = prompt_loader.load("relevance_check")
    llm = ChatOpenAI(model="gpt-4.1-mini").with_structured_output(RelevanceCheckOutput)
    prompt_template = ChatPromptTemplate.from_messages([("system", prompt)])
    return (prompt_template | llm).invoke({"input_search_results": results})

# ========== SCRAPING & CLEANING ==========
def convert_html_to_markdown(html):
    soup = BeautifulSoup(html, "html.parser")
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        h.replace_with(f"{'#'*int(h.name[1])} {h.get_text()}\n\n")
    for tag, fmt in [("a", "[{0}]({1})"), ("b", "**{0}**"), ("strong", "**{0}**"),
                     ("i", "*{0}*"), ("em", "*{0}*")]:
        for t in soup.find_all(tag):
            val = fmt.format(t.get_text(), t.get("href", "")) if tag == "a" else fmt.format(t.get_text())
            t.replace_with(val)
    for ul in soup.find_all("ul"):
        for li in ul.find_all("li"):
            li.replace_with(f"- {li.get_text()}\n")
    for ol in soup.find_all("ol"):
        for i, li in enumerate(ol.find_all("li"), 1):
            li.replace_with(f"{i}. {li.get_text()}\n")
    return re.sub(r"\n\s*\n", "\n\n", soup.get_text().strip())

def scrape_markdown(relevant_results):
    markdowns = []
    for result in relevant_results:
        url = result["link"]
        response = requests.get(
            "https://scraping.narf.ai/api/v1/",
            params={
                "api_key": st.secrets["SCRAPING_API_KEY"],
                "url": url,
                "render_js": "true",
            },
        )
        if response.ok:
            md = convert_html_to_markdown(response.text)
            markdowns.append({
                "url": url,
                "title": result["title"],
                "id": result["id"],
                "markdown": md,
            })
    return markdowns

# ========== SUMMARIZATION ==========
def summarize_pages(markdowns, prompt_loader: PromptLoader):
    llm = ChatOpenAI(model="gpt-4.1-mini")
    prompt = prompt_loader.load("summarise_markdown_page")
    chain = ChatPromptTemplate.from_messages([("system", prompt)]) | llm
    summaries = []
    for m in markdowns:
        try:
            summary = chain.invoke({"markdown_input": " ".join(m["markdown"].split()[:2000])})
            summaries.append({"markdown_summary": summary.content, "url": m["url"]})
        except:
            continue
    return summaries

# ========== REVIEW + EMAIL ==========
def run_review_graph(summaries, prompt_loader: PromptLoader):
    email_template = prompt_loader.load("email_template")
    llm = ChatOpenAI(model="gpt-4.1-mini")

    class SummariserOutput(BaseModel):
        email_summary: str
        message: str

    summariser = ChatPromptTemplate.from_messages([
        ("system", prompt_loader.load("summariser")),
        ("placeholder", "{messages}")
    ])
    summariser_chain = summariser | llm.with_structured_output(SummariserOutput)

    def summariser_fn(state: State):
        out = summariser_chain.invoke({
            "messages": state["messages"],
            "list_of_summaries": state["summaries"],
            "input_template": email_template,
        })
        return {
            "messages": [AIMessage(content=out.email_summary), AIMessage(content=out.message)],
            "created_summaries": [out.email_summary],
        }

    class ReviewerOutput(BaseModel):
        approved: bool
        message: str

    reviewer = ChatPromptTemplate.from_messages([
        ("system", prompt_loader.load("reviewer")),
        ("placeholder", "{messages}")
    ])
    reviewer_chain = reviewer | llm.with_structured_output(ReviewerOutput)

    def reviewer_fn(state: State):
        msgs = [
            HumanMessage(content=m.content) if isinstance(m, AIMessage) else AIMessage(content=m.content)
            for m in state["messages"]
        ]
        state["messages"] = msgs
        out = reviewer_chain.invoke({"messages": state["messages"]})
        return {
            "messages": [HumanMessage(content=out.message)],
            "approved": out.approved,
        }

    def decide(state: State):
        return END if state["approved"] else "summariser"

    builder = StateGraph(State)
    builder.add_node("summariser", summariser_fn)
    builder.add_node("reviewer", reviewer_fn)
    builder.add_edge(START, "summariser")
    builder.add_edge("summariser", "reviewer")
    builder.add_conditional_edges("reviewer", decide)

    graph = builder.compile()
    out = graph.invoke({"summaries": summaries})
    return out["created_summaries"][-1]

def send_email(to_email, to_name, email_content):
    config = sib_api_v3_sdk.Configuration()
    config.api_key["api-key"] = st.secrets["SENDINGBLUE_API_KEY"]
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(config))
    personalized_content = f"""
        <html>
        <body>
            <p>Dear {to_name},</p>
            <p>We hope you're doing well!</p>
            {email_content}
            <p>Thank you for choosing Lunathink. We're honored to support your research journey.</p>
            <p>Warm regards,<br>Lunathink Team</p>
        </body>
        </html>
        """
    email_params = {
        "subject": "Your Personalized AI Research Summary",
        "sender": {"name": "Lunathink", "email": "apikey214@gmail.com"},
        "html_content": personalized_content,
        "to": [{"email": to_email, "name": to_name}],
    }
    try:
        api_instance.send_transac_email(sib_api_v3_sdk.SendSmtpEmail(**email_params))
    except ApiException as e:
        raise RuntimeError(f"Email sending failed: {e}")

# ========== MAIN ENTRY ==========
def run_pipeline(search_terms: List[str], name: str, email: str, profile: str):
    prompt_loader = PromptLoader(profile)
    relevant_results = []
    for search_term in search_terms:
        search_results = search_serper(search_term)
        relevance_output = check_search_relevance(search_results, prompt_loader)
        relevant_ids = [r.id for r in relevance_output.relevant_results]
        filtered_results = [r for r in search_results if str(r["id"]) in relevant_ids]
        relevant_results.extend(filtered_results)

    markdowns = scrape_markdown(relevant_results)
    summaries = summarize_pages(markdowns, prompt_loader)
    email_summary = run_review_graph(summaries, prompt_loader)
    send_email(email, name, email_summary)
    return email_summary

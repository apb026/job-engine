"""
app.py
Career Copilot - single-user Streamlit app.

Pages:
- Generate: paste/receive a JD, get tailored resume + cover letter + scores
- Review history: past generations, edit and save final versions
- Tracker: simple application status board
- Memory: view/add/deactivate source items, manage writing preferences

Run locally:
    cd app
    streamlit run app.py

On Streamlit Cloud, set these secrets (.streamlit/secrets.toml or the
dashboard's Secrets manager):
    GROQ_API_KEY = "..."
    GITHUB_TOKEN = "..."        # optional, enables backup/restore
    GITHUB_REPO = "you/career-copilot-data"
    GITHUB_BRANCH = "main"
"""

import os
import json
import streamlit as st

import backup

DB_PATH = os.environ.get("CAREER_DB_PATH", "data/career.db")

# Pull secrets into env vars so the rest of the app (llm.py, backup.py)
# can read them the same way locally and on Streamlit Cloud.
for key in ("GROQ_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_BRANCH"):
    if key in st.secrets and key not in os.environ:
        os.environ[key] = st.secrets[key]

# Restore DB from GitHub backup before anything else touches it.
if "db_restored" not in st.session_state:
    restored, msg = backup.restore_db(DB_PATH)
    st.session_state["db_restored"] = True
    st.session_state["restore_message"] = msg

import db
import memory
import pipeline

db.init_db()


def do_backup():
    ok, msg = backup.backup_db(DB_PATH)
    if not ok and "not configured" not in msg:
        st.warning(f"Backup issue: {msg}")


st.set_page_config(page_title="Career Copilot", layout="wide")
st.title("Career Copilot")

if st.session_state.get("restore_message"):
    st.caption(st.session_state["restore_message"])

page = st.sidebar.radio(
    "Navigate",
    ["Generate", "History", "Tracker", "Memory", "Preferences"],
)

# ---------------------------------------------------------------- Generate
if page == "Generate":
    st.header("Tailor a resume + cover letter for a job")

    incoming = st.session_state.pop("incoming_jd", None)

    jd_text = st.text_area(
        "Paste the job description",
        value=incoming.get("text", "") if incoming else "",
        height=250,
        placeholder="Paste the full job posting text here...",
    )
    col1, col2 = st.columns(2)
    with col1:
        url = st.text_input("Job URL (optional)", value=incoming.get("url", "") if incoming else "")
    with col2:
        platform = st.selectbox(
            "Platform",
            ["", "linkedin", "indeed", "handshake", "greenhouse", "lever", "ashby", "workday", "other"],
            index=0,
        )

    max_pages = st.radio("Max resume length", [1, 2], horizontal=True)

    if st.button("Generate tailored resume + cover letter", type="primary"):
        if not jd_text.strip():
            st.error("Paste a job description first.")
        else:
            with st.spinner("Parsing job description..."):
                jd_id, parsed = pipeline.ingest_job_description(
                    jd_text, url=url or None, source_platform=platform or None
                )
            st.success(f"Parsed: {parsed.get('title') or 'unknown title'} at {parsed.get('company') or 'unknown company'}")

            with st.spinner("Retrieving relevant background and generating..."):
                result = pipeline.generate_for_job(jd_id, max_pages=max_pages)
            do_backup()
            st.session_state["last_result"] = result

    result = st.session_state.get("last_result")
    if result:
        st.divider()
        truth = result["truth_check"]
        ats = result["ats_score"]
        gap = result["skill_gap"]

        m1, m2, m3 = st.columns(3)
        m1.metric("ATS score", ats.get("score", "n/a"))
        m2.metric("Truth check", "Passed" if truth.get("passed") else "FLAGGED")
        m3.metric("Retrieved sources used", result["retrieved_count"])

        if not truth.get("passed"):
            st.error("Some claims could not be verified against your stored background:")
            for fc in truth.get("flagged_claims", []):
                st.write(f"- **{fc.get('claim')}**: {fc.get('reason')}")
            st.caption("Review the resume below carefully before sending it.")

        if gap.get("missing_skills"):
            st.warning("Skills in the JD not found in your background: " + ", ".join(gap["missing_skills"]))

        st.subheader("Tailored resume")
        edited_resume = st.text_area("Edit before saving/using", value=result["resume_markdown"], height=400, key="resume_edit_area")
        if st.button("Save my edits to this resume version"):
            pipeline.save_user_edit(resume_id=result["resume_id"], final_text=edited_resume)
            do_backup()
            st.success("Saved.")

        st.subheader("Cover letter")
        edited_cover = st.text_area("Edit before saving/using", value=result["cover_letter"], height=250, key="cover_edit_area")
        if st.button("Save my edits to this cover letter"):
            pipeline.save_user_edit(cover_letter_id=result["cover_letter_id"], final_text=edited_cover)
            do_backup()
            st.success("Saved.")

        st.divider()
        st.subheader("Mark application status")
        status = st.selectbox("Status", ["drafted", "applied", "referred", "interview", "rejected", "ghosted", "offer"])
        notes = st.text_input("Notes (recruiter name, referral source, etc.)")
        if st.button("Update tracker"):
            pipeline.update_application_status(result["jd_id"], result["resume_id"], status, notes)
            do_backup()
            st.success("Tracker updated.")

# ----------------------------------------------------------------- History
elif page == "History":
    st.header("Past generations")
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT rv.id as resume_id, jd.company, jd.title, jd.created_at, rv.ats_score,
                  rv.truth_check_passed, cl.id as cover_letter_id
           FROM resume_versions rv
           JOIN job_descriptions jd ON jd.id = rv.job_description_id
           LEFT JOIN cover_letters cl ON cl.resume_version_id = rv.id
           ORDER BY rv.created_at DESC"""
    ).fetchall()
    conn.close()

    if not rows:
        st.info("Nothing generated yet.")
    for r in rows:
        with st.expander(f"{r['title'] or 'Untitled'} at {r['company'] or 'Unknown'} — {r['created_at'][:10]}"):
            st.write(f"ATS score: {r['ats_score']} | Truth check passed: {bool(r['truth_check_passed'])}")
            conn = db.get_connection()
            rv = conn.execute("SELECT * FROM resume_versions WHERE id = ?", (r["resume_id"],)).fetchone()
            cl = None
            if r["cover_letter_id"]:
                cl = conn.execute("SELECT * FROM cover_letters WHERE id = ?", (r["cover_letter_id"],)).fetchone()
            conn.close()

            st.markdown("**Resume (final edited version if saved, otherwise original generation):**")
            st.markdown(rv["user_edited_final"] or rv["content_markdown"])
            if cl:
                st.markdown("**Cover letter:**")
                st.markdown(cl["user_edited_final"] or cl["content"])

# ----------------------------------------------------------------- Tracker
elif page == "Tracker":
    st.header("Application tracker")
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT a.id, jd.company, jd.title, a.status, a.notes, a.updated_at
           FROM applications a
           JOIN job_descriptions jd ON jd.id = a.job_description_id
           ORDER BY a.updated_at DESC"""
    ).fetchall()
    conn.close()

    if not rows:
        st.info("No applications tracked yet. Generate a resume and set its status to start tracking.")
    else:
        st.dataframe(
            [
                {
                    "Company": r["company"],
                    "Title": r["title"],
                    "Status": r["status"],
                    "Notes": r["notes"],
                    "Updated": r["updated_at"][:10],
                }
                for r in rows
            ],
            use_container_width=True,
        )

# ------------------------------------------------------------------ Memory
elif page == "Memory":
    st.header("Career memory")
    st.caption("This is the only source of truth the system is allowed to draw from when writing your resume.")

    item_type_filter = st.selectbox("Filter by type", ["all", "bullet", "project", "skill", "cert", "summary"])
    items = memory.all_active_items(None if item_type_filter == "all" else item_type_filter)

    st.write(f"{len(items)} active item(s).")
    for item in items:
        cols = st.columns([5, 1])
        with cols[0]:
            label = f" ({item['source_label']})" if item["source_label"] else ""
            st.markdown(f"**[{item['item_type']}]**{label}: {item['content']}")
        with cols[1]:
            if st.button("Remove", key=f"remove_{item['id']}"):
                memory.deactivate_item(item["id"])
                do_backup()
                st.rerun()

    st.divider()
    st.subheader("Add a new memory item")
    new_type = st.selectbox("Type", ["bullet", "project", "skill", "cert", "summary"], key="new_item_type")
    new_label = st.text_input("Source label (e.g. company/role, optional)", key="new_item_label")
    new_content = st.text_area("Content", key="new_item_content")
    new_tags = st.text_input("Tags (comma separated, optional)", key="new_item_tags")
    if st.button("Add to memory"):
        if new_content.strip():
            tags = [t.strip() for t in new_tags.split(",") if t.strip()]
            memory.add_source_item(new_type, new_content.strip(), new_label.strip() or None, tags)
            do_backup()
            st.success("Added.")
            st.rerun()
        else:
            st.error("Content can't be empty.")

# -------------------------------------------------------------- Preferences
elif page == "Preferences":
    st.header("Writing preferences")
    st.caption(
        "Free-text rules the system should always follow, e.g. "
        "'always say STEM OPT eligible, never say need sponsorship' or "
        "'prefer the word built over developed'."
    )

    prefs = memory.get_active_preferences()
    for p in prefs:
        st.write(f"- {p}")

    new_pref = st.text_input("Add a preference")
    if st.button("Add preference"):
        if new_pref.strip():
            memory.add_writing_preference(new_pref.strip())
            do_backup()
            st.success("Added.")
            st.rerun()

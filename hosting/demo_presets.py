"""Demo instance presets for the hosting platform.

Each preset defines a themed BananaWiki instance with pre-populated articles,
kanban boards, and canvas layouts.  Content is written in English for
presentation / demo purposes.

A preset is a dict with:
    id: unique short identifier
    name: display name (English)
    description: one-line summary (English)
    articles: list of (title, slug, content_markdown)
    kanban: list of board dicts, each with title, description, and
                  columns (list of column dicts with title and tickets)
    canvases: list of canvas dicts with title, slug, description,
                  and data (JSON-serialisable dict with nodes/edges)
"""

import json


def _canvas_data(nodes, edges=None):
    """Return a JSON string for canvas__layouts.data."""
    return json.dumps({
        "nodes": nodes,
        "edges": edges or [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }, ensure_ascii=False)


PRESET_AMMINISTRAZIONE = {
    "id": "administration",
    "name": "Administration Wiki",
    "description": "Attendance management, members, billing, staff and schedules",
    "articles": [
        (
            "Welcome: Administration Wiki",
            "admin-welcome",
            (
                "# Welcome to the Administration Wiki\n\n"
                "This wiki centralises all the operational information for the "
                "organisation. Here you will find:\n\n"
                "- **Daily attendance**: register updated every day\n"
                "- **Member list**: complete registry with payment status\n"
                "- **Billing**: deadlines, issued and pending invoices\n"
                "- **Staff**: roles, contacts and shifts\n"
                "- **Schedules**: weekly planning and public holidays\n\n"
                "Use the sidebar to navigate between categories.\n\n"
                "---\n\n"
                "*Last updated: maintained by the administrative team.*"
            ),
        ),
        (
            "Attendance Register",
            "attendance-register",
            (
                "# Attendance Register\n\n"
                "## How it works\n\n"
                "Each operator records their attendance in the morning through "
                "this page. The format is:\n\n"
                "| Date | Name | Check-in | Check-out | Notes |\n"
                "|---|---|---|---|---|\n"
                "| 2025-05-05 | Mario Rossi | 08:30 | 17:00 | - |\n"
                "| 2025-05-05 | Laura Bianchi | 09:00 | 18:00 | Afternoon meeting |\n"
                "| 2025-05-05 | Giuseppe Verdi | 08:00 | 16:30 | Morning shift |\n\n"
                "## Rules\n\n"
                "1. Record attendance **by 09:30**\n"
                "2. Any absences must be reported to the manager\n"
                "3. Changes must be approved by the admin\n\n"
                "## Monthly statistics\n\n"
                "- Total attendance in May: **142 out of 160 expected**\n"
                "- Attendance rate: **88.7%**\n"
                "- Justified absences: 12 | Unjustified: 6"
            ),
        ),
        (
            "Members and Payments",
            "members-and-payments",
            (
                "# Members and Payments\n\n"
                "## Active members\n\n"
                "| # | Name | Enrolment date | Fee | Status |\n"
                "|---|---|---|---|---|\n"
                "| 1 | Anna Moretti | 2025-01-15 | 150 EUR | Paid |\n"
                "| 2 | Luca Ferraro | 2025-02-01 | 150 EUR | Pending |\n"
                "| 3 | Chiara Colombo | 2025-01-20 | 150 EUR | Paid |\n"
                "| 4 | Marco De Luca | 2025-03-10 | 150 EUR | Overdue |\n"
                "| 5 | Sofia Ricci | 2025-02-28 | 150 EUR | Paid |\n\n"
                "## Financial summary\n\n"
                "- Total members: **48**\n"
                "- Fees collected: **6,450 EUR**\n"
                "- Fees pending: **750 EUR**\n"
                "- Fees overdue: **600 EUR**\n\n"
                "## Reminder procedure\n\n"
                "1. After 7 days from the due date → reminder email\n"
                "2. After 14 days → second notice\n"
                "3. After 30 days → access suspension"
            ),
        ),
        (
            "Staff and Shifts",
            "staff-and-shifts",
            (
                "# Staff and Shifts\n\n"
                "## Organisational chart\n\n"
                "| Role | Name | Email | Extension |\n"
                "|---|---|---|---|\n"
                "| Director | Maria Esposito | m.esposito@example.org | 101 |\n"
                "| Coordinator | Paolo Romano | p.romano@example.org | 102 |\n"
                "| Secretary | Anna Greco | a.greco@example.org | 103 |\n"
                "| Instructor | Luca Marino | l.marino@example.org | 104 |\n"
                "| Instructor | Sara Fontana | s.fontana@example.org | 105 |\n\n"
                "## Weekly shifts\n\n"
                "| Day | Morning (8:00–13:00) | Afternoon (14:00–19:00) |\n"
                "|---|---|---|\n"
                "| Monday | Paolo, Luca | Maria, Sara |\n"
                "| Tuesday | Maria, Anna | Paolo, Luca |\n"
                "| Wednesday | Luca, Sara | Anna, Paolo |\n"
                "| Thursday | Paolo, Maria | Sara, Luca |\n"
                "| Friday | Anna, Sara | Maria, Paolo |\n\n"
                "## Important notes\n\n"
                "- Shift changes must be agreed at least **48 hours** in advance\n"
                "- Each employee is entitled to **2 days off** per week"
            ),
        ),
        (
            "Billing and Deadlines",
            "billing-and-deadlines",
            (
                "# Billing and Deadlines\n\n"
                "## Current month invoices\n\n"
                "| Invoice No. | Client | Amount | Due date | Status |\n"
                "|---|---|---|---|---|\n"
                "| FT-2025-041 | Azienda Alfa Srl | 2,400 EUR | 15/05/2025 | Issued |\n"
                "| FT-2025-042 | Studio Beta | 1,800 EUR | 20/05/2025 | Issued |\n"
                "| FT-2025-043 | Coop. Gamma | 3,200 EUR | 25/05/2025 | To be issued |\n"
                "| FT-2025-044 | Private D. Rossi | 450 EUR | 30/05/2025 | Issued |\n\n"
                "## Quarterly summary\n\n"
                "- Q1 2025 revenue: **24,600 EUR**\n"
                "- Q2 revenue (partial): **7,850 EUR**\n"
                "- Annual forecast: **~95,000 EUR**\n\n"
                "## Invoice issuance procedure\n\n"
                "1. Verify client details in the registry\n"
                "2. Create the invoice in the management system\n"
                "3. Send via certified email within 12 days of the transaction\n"
                "4. Update this page with the status"
            ),
        ),
    ],
    "kanban": [
        {
            "title": "Administrative Activities",
            "description": "Management of daily activities in the administrative office",
            "columns": [
                {
                    "title": "To Do",
                    "tickets": [
                        {"title": "Chase overdue payments", "description": "Send reminder emails to the 3 members with overdue fees", "priority": "high"},
                        {"title": "Update attendance register", "description": "Enter the week's attendance records in the digital register", "priority": "medium"},
                        {"title": "Prepare monthly report", "description": "Compile the attendance and performance report for management", "priority": "medium"},
                    ],
                },
                {
                    "title": "In Progress",
                    "tickets": [
                        {"title": "Issue invoice FT-2025-043", "description": "Invoice for Coop. Gamma: verify amount and tax details", "priority": "high"},
                        {"title": "Plan June shifts", "description": "Define staff shifts for June taking holidays into account", "priority": "medium"},
                    ],
                },
                {
                    "title": "In Review",
                    "tickets": [
                        {"title": "Review new member documents", "description": "Verify identity documents and enrolment forms for 5 new members", "priority": "low"},
                    ],
                },
                {
                    "title": "Completed",
                    "tickets": [
                        {"title": "Send April invoices", "description": "All April invoices have been issued and sent via certified email", "priority": "low"},
                        {"title": "Update member registry", "description": "Member registry updated with new contacts and details", "priority": "low"},
                    ],
                },
            ],
        },
    ],
    "canvases": [
        {
            "title": "Company Organisational Chart",
            "slug": "org-chart",
            "description": "Hierarchical structure of the organisation",
            "data": _canvas_data(
                nodes=[
                    {"id": "n1", "type": "sticky", "position": {"x": 300, "y": 50}, "data": {"label": "General Management\nMaria Esposito", "color": "#fbbf24"}},
                    {"id": "n2", "type": "sticky", "position": {"x": 100, "y": 200}, "data": {"label": "Coordination\nPaolo Romano", "color": "#60a5fa"}},
                    {"id": "n3", "type": "sticky", "position": {"x": 300, "y": 200}, "data": {"label": "Secretariat\nAnna Greco", "color": "#60a5fa"}},
                    {"id": "n4", "type": "sticky", "position": {"x": 500, "y": 200}, "data": {"label": "Training\nLuca Marino", "color": "#60a5fa"}},
                    {"id": "n5", "type": "sticky", "position": {"x": 100, "y": 350}, "data": {"label": "Instructor\nSara Fontana", "color": "#34d399"}},
                    {"id": "n6", "type": "sticky", "position": {"x": 300, "y": 350}, "data": {"label": "Assistant\nGiulia Conti", "color": "#34d399"}},
                ],
                edges=[
                    {"id": "e1", "source": "n1", "target": "n2"},
                    {"id": "e2", "source": "n1", "target": "n3"},
                    {"id": "e3", "source": "n1", "target": "n4"},
                    {"id": "e4", "source": "n2", "target": "n5"},
                    {"id": "e5", "source": "n3", "target": "n6"},
                ],
            ),
        },
    ],
}


PRESET_STUDENTI = {
    "id": "students",
    "name": "Students Wiki",
    "description": "Information on courses, events, tutorials and resources for students",
    "articles": [
        (
            "Welcome: Student Area",
            "students-welcome",
            (
                "# Welcome to the Student Area\n\n"
                "This wiki is your reference point for everything related to "
                "courses, training days and learning resources.\n\n"
                "## What you'll find here\n\n"
                "- **Course calendar**: dates, times and classrooms\n"
                "- **Learning materials**: handouts, links and tutorials\n"
                "- **Practical tutorials**: step-by-step guides (coding, design, etc.)\n"
                "- **FAQ**: answers to the most common questions\n"
                "- **Notice board**: important communications\n\n"
                "## How to use the wiki\n\n"
                "1. Navigate using the **sidebar** on the left\n"
                "2. Use **search** to find specific topics\n"
                "3. Check the **Kanban Board** to see project status\n"
                "4. Explore **Canvases** for diagrams and concept maps\n\n"
                "---\n\n"
                "*Happy studying!*"
            ),
        ),
        (
            "Course Calendar",
            "course-calendar",
            (
                "# Course Calendar: May 2025\n\n"
                "## Weekly courses\n\n"
                "| Day | Time | Course | Instructor | Room |\n"
                "|---|---|---|---|---|\n"
                "| Monday | 09:00–12:00 | Introduction to Programming | Prof. Neri | Lab A |\n"
                "| Monday | 14:00–16:00 | Web Design Fundamentals | Prof. Ricci | Lab B |\n"
                "| Tuesday | 10:00–13:00 | Databases and SQL | Prof. Bianchi | Lab A |\n"
                "| Wednesday | 09:00–11:00 | English Language B2 | Prof. Smith | Room 3 |\n"
                "| Thursday | 14:00–17:00 | Practical Project: Mobile App | Prof. Verdi | Lab C |\n"
                "| Friday | 09:00–12:00 | Cybersecurity | Prof. Russo | Lab A |\n\n"
                "## Important dates\n\n"
                "- **12 May**: Interim project submission\n"
                "- **19 May**: SQL assessment test\n"
                "- **26 May**: Final project presentations\n"
                "- **30 May**: Last day of classes"
            ),
        ),
        (
            "Tutorial: Introduction to Coding",
            "tutorial-coding",
            (
                "# Tutorial: Introduction to Coding\n\n"
                "## What is programming?\n\n"
                "Programming is the art of writing instructions that a computer "
                "can execute. In this tutorial you will learn the basics using **Python**.\n\n"
                "## First program: Hello World\n\n"
                "```python\n"
                "print(\"Hello, world!\")\n"
                "```\n\n"
                "## Variables and data types\n\n"
                "```python\n"
                "name = \"Mario\"           # string\n"
                "age = 25                  # integer\n"
                "grade_avg = 27.5          # float\n"
                "enrolled = True           # boolean\n\n"
                "print(f\"{name} is {age} years old\")\n"
                "```\n\n"
                "## Control structures\n\n"
                "```python\n"
                "# Condition\n"
                "if grade_avg >= 18:\n"
                "    print(\"Passed!\")\n"
                "else:\n"
                "    print(\"Try again\")\n\n"
                "# Loop\n"
                "for i in range(5):\n"
                "    print(f\"Iteration {i}\")\n"
                "```\n\n"
                "## Exercises\n\n"
                "1. Write a program that calculates the average of 3 numbers\n"
                "2. Create a function that checks whether a number is even or odd\n"
                "3. Build a simple \"Guess the number\" game\n\n"
                "## Additional resources\n\n"
                "- [Python.org](https://python.org): official documentation\n"
                "- [W3Schools Python](https://w3schools.com/python): interactive tutorial"
            ),
        ),
        (
            "FAQ: Frequently Asked Questions",
            "faq-students",
            (
                "# FAQ: Frequently Asked Questions\n\n"
                "## Access and account\n\n"
                "**How do I access the wiki?**\n"
                "Use the credentials provided at enrolment. If you have forgotten "
                "them, contact the secretary's office.\n\n"
                "**Can I edit pages?**\n"
                "Yes, students with the *editor* role can create and edit "
                "pages in their own area.\n\n"
                "## Courses and materials\n\n"
                "**Where do I find the lecture slides?**\n"
                "On the corresponding course page, in the \"Materials\" section.\n\n"
                "**Can I download materials as PDF?**\n"
                "Yes, each page has an **Export PDF** button in the top bar.\n\n"
                "## Projects and submissions\n\n"
                "**How do I submit my project?**\n"
                "Upload the file in the attachments section of the project page "
                "or use the Kanban Board to update the status.\n\n"
                "**Can I work in a group?**\n"
                "Yes, groups are assigned by the instructor. Use the internal "
                "chat to coordinate with your classmates."
            ),
        ),
    ],
    "kanban": [
        {
            "title": "Student Projects",
            "description": "Progress of the semester's educational projects",
            "columns": [
                {
                    "title": "Backlog",
                    "tickets": [
                        {"title": "Research thesis topic", "description": "Choose the topic for the end-of-course thesis", "priority": "medium"},
                        {"title": "Set up development environment", "description": "Install Python, VS Code and Git on your computer", "priority": "high"},
                    ],
                },
                {
                    "title": "In Progress",
                    "tickets": [
                        {"title": "Mobile App, UI prototype", "description": "Create wireframes and user interface prototype with Figma", "priority": "high"},
                        {"title": "SQL exercises chapter 3", "description": "Complete the join and subquery exercises from chapter 3", "priority": "medium"},
                    ],
                },
                {
                    "title": "To Submit",
                    "tickets": [
                        {"title": "Cybersecurity report", "description": "Write the report on web vulnerability analysis (min. 2000 words)", "priority": "high"},
                    ],
                },
                {
                    "title": "Submitted",
                    "tickets": [
                        {"title": "Hello World in Python", "description": "First programming exercise completed and submitted", "priority": "low"},
                        {"title": "Web Design presentation", "description": "Slides on the history of web design submitted on 28 April", "priority": "low"},
                    ],
                },
            ],
        },
    ],
    "canvases": [
        {
            "title": "Learning Path Map",
            "slug": "learning-path",
            "description": "Overview of the educational path and skills",
            "data": _canvas_data(
                nodes=[
                    {"id": "n1", "type": "sticky", "position": {"x": 300, "y": 50}, "data": {"label": "Foundations\nLogic and Algorithms", "color": "#fbbf24"}},
                    {"id": "n2", "type": "sticky", "position": {"x": 100, "y": 200}, "data": {"label": "Programming\nPython", "color": "#60a5fa"}},
                    {"id": "n3", "type": "sticky", "position": {"x": 300, "y": 200}, "data": {"label": "Databases\nSQL & Modelling", "color": "#60a5fa"}},
                    {"id": "n4", "type": "sticky", "position": {"x": 500, "y": 200}, "data": {"label": "Web\nHTML/CSS/JS", "color": "#60a5fa"}},
                    {"id": "n5", "type": "sticky", "position": {"x": 200, "y": 370}, "data": {"label": "Mobile App\nPractical Project", "color": "#34d399"}},
                    {"id": "n6", "type": "sticky", "position": {"x": 400, "y": 370}, "data": {"label": "Security\nCybersecurity", "color": "#f87171"}},
                    {"id": "n7", "type": "sticky", "position": {"x": 300, "y": 520}, "data": {"label": "Final Project\nPresentation", "color": "#a78bfa"}},
                ],
                edges=[
                    {"id": "e1", "source": "n1", "target": "n2"},
                    {"id": "e2", "source": "n1", "target": "n3"},
                    {"id": "e3", "source": "n1", "target": "n4"},
                    {"id": "e4", "source": "n2", "target": "n5"},
                    {"id": "e5", "source": "n3", "target": "n5"},
                    {"id": "e6", "source": "n4", "target": "n6"},
                    {"id": "e7", "source": "n5", "target": "n7"},
                    {"id": "e8", "source": "n6", "target": "n7"},
                ],
            ),
        },
    ],
}


PRESET_AZIENDA = {
    "id": "company",
    "name": "Company Wiki: Internal Team",
    "description": "Internal knowledge base for business processes, onboarding and procedures",
    "articles": [
        (
            "Welcome: Company Wiki",
            "company-welcome",
            (
                "# Welcome to the Company Wiki\n\n"
                "This is the company's internal knowledge base. Here you will find "
                "all operational procedures, shared documents and guidelines "
                "for each department.\n\n"
                "## Departments\n\n"
                "- **Human Resources**: onboarding, policies, holidays and leave\n"
                "- **IT & Development**: technical guides, architecture, deployment\n"
                "- **Marketing**: brand guidelines, editorial plans, templates\n"
                "- **Sales**: price lists, CRM, commercial procedures\n\n"
                "## Usage rules\n\n"
                "1. Each employee is responsible for the content they publish\n"
                "2. Confidential information must be marked as **protected**\n"
                "3. Update pages when procedures change\n"
                "4. Use the Kanban Board to track team tasks"
            ),
        ),
        (
            "New Employee Onboarding",
            "onboarding",
            (
                "# Onboarding: Guide for New Employees\n\n"
                "## First day\n\n"
                "- [ ] Collect badge and office keys\n"
                "- [ ] Set up workstation (PC, monitor, keyboard)\n"
                "- [ ] Install software: Slack, Git, VS Code, Docker\n"
                "- [ ] Create wiki account and log in\n"
                "- [ ] Read company policies (page *Policies and Regulations*)\n\n"
                "## First week\n\n"
                "- [ ] Meet your team leader for the integration plan\n"
                "- [ ] Complete mandatory security training courses\n"
                "- [ ] Set up two-factor authentication on all accounts\n"
                "- [ ] Read the architecture of current projects\n\n"
                "## First month\n\n"
                "- [ ] Complete the first assigned task\n"
                "- [ ] Participate in a code review\n"
                "- [ ] Feedback meeting with the HR manager\n\n"
                "## Useful contacts\n\n"
                "| Who | Email |\n"
                "|---|---|\n"
                "| HR Manager | hr@example.com |\n"
                "| IT Support | support@example.com |\n"
                "| Facility | facility@example.com |"
            ),
        ),
        (
            "Technical Architecture",
            "technical-architecture",
            (
                "# Technical Architecture\n\n"
                "## Technology stack\n\n"
                "| Component | Technology |\n"
                "|---|---|\n"
                "| Backend | Python 3.11 + Flask |\n"
                "| Frontend | React 18 + TypeScript |\n"
                "| Database | PostgreSQL 16 |\n"
                "| Cache | Redis 7 |\n"
                "| CI/CD | GitHub Actions |\n"
                "| Hosting | AWS (ECS + RDS) |\n\n"
                "## Environments\n\n"
                "- **Development**: localhost, local database\n"
                "- **Staging**: staging.example.com, automatic deployment on merge to `develop`\n"
                "- **Production**: app.example.com, manual deployment with approval\n\n"
                "## Code conventions\n\n"
                "- Branch naming: `feature/TICKET-123-description`\n"
                "- Commits: [Conventional Commits](https://conventionalcommits.org)\n"
                "- Code review required before merge\n"
                "- Minimum test coverage: **80%**"
            ),
        ),
        (
            "Policies and Regulations",
            "policies-regulations",
            (
                "# Company Policies and Regulations\n\n"
                "## Working hours\n\n"
                "- Flexible schedule: arrival between **8:00** and **10:00**\n"
                "- Core hours: **10:00–16:00** (mandatory presence)\n"
                "- Lunch break: **1 hour** (unpaid)\n"
                "- Remote work: up to **3 days** per week\n\n"
                "## Holidays and leave\n\n"
                "- Annual leave: **26** working days\n"
                "- Paid leave: **32 hours** per year\n"
                "- Sick leave: medical certificate from the **first day**\n"
                "- Holiday requests: at least **2 weeks** in advance via HR\n\n"
                "## IT security\n\n"
                "- Password: minimum 12 characters, change every 90 days\n"
                "- Mandatory 2FA on all company accounts\n"
                "- Do not share credentials via chat or email\n"
                "- Report any security incident to IT immediately"
            ),
        ),
    ],
    "kanban": [
        {
            "title": "Current Sprint: Dev Team",
            "description": "Tasks for the current sprint for the development team",
            "columns": [
                {
                    "title": "To Do",
                    "tickets": [
                        {"title": "Implement push notification API", "description": "REST endpoint for sending push notifications via Firebase", "priority": "high"},
                        {"title": "Update Python dependencies", "description": "Update Flask, SQLAlchemy and pytest to the latest stable versions", "priority": "medium"},
                        {"title": "Write tests for auth module", "description": "Achieve 90% coverage on the authentication module", "priority": "medium"},
                    ],
                },
                {
                    "title": "In Progress",
                    "tickets": [
                        {"title": "DB migration to PostgreSQL 16", "description": "Complete the database migration from v15 to v16 with zero downtime", "priority": "critical"},
                        {"title": "Redesign user profile page", "description": "Implement the new UI/UX design approved by the design team", "priority": "medium"},
                    ],
                },
                {
                    "title": "Code Review",
                    "tickets": [
                        {"title": "Fix SSO login bug", "description": "PR #452: fixed redirect loop in SSO flow with Azure AD", "priority": "high"},
                    ],
                },
                {
                    "title": "Done",
                    "tickets": [
                        {"title": "Grafana monitoring setup", "description": "Grafana dashboard configured with CPU, RAM and API latency metrics", "priority": "medium"},
                        {"title": "API v2 documentation", "description": "Swagger/OpenAPI spec updated for all v2 endpoints", "priority": "low"},
                    ],
                },
            ],
        },
    ],
    "canvases": [
        {
            "title": "Microservices Architecture",
            "slug": "microservices-architecture",
            "description": "Diagram of microservices and their interactions",
            "data": _canvas_data(
                nodes=[
                    {"id": "n1", "type": "sticky", "position": {"x": 300, "y": 50}, "data": {"label": "API Gateway\nNginx + Rate Limiting", "color": "#fbbf24"}},
                    {"id": "n2", "type": "sticky", "position": {"x": 100, "y": 220}, "data": {"label": "Auth Service\nJWT + OAuth2", "color": "#f87171"}},
                    {"id": "n3", "type": "sticky", "position": {"x": 300, "y": 220}, "data": {"label": "User Service\nUser CRUD", "color": "#60a5fa"}},
                    {"id": "n4", "type": "sticky", "position": {"x": 500, "y": 220}, "data": {"label": "Notification Service\nEmail + Push", "color": "#60a5fa"}},
                    {"id": "n5", "type": "sticky", "position": {"x": 200, "y": 400}, "data": {"label": "PostgreSQL\nPrimary Database", "color": "#34d399"}},
                    {"id": "n6", "type": "sticky", "position": {"x": 400, "y": 400}, "data": {"label": "Redis\nCache + Sessions", "color": "#a78bfa"}},
                ],
                edges=[
                    {"id": "e1", "source": "n1", "target": "n2"},
                    {"id": "e2", "source": "n1", "target": "n3"},
                    {"id": "e3", "source": "n1", "target": "n4"},
                    {"id": "e4", "source": "n2", "target": "n5"},
                    {"id": "e5", "source": "n3", "target": "n5"},
                    {"id": "e6", "source": "n2", "target": "n6"},
                    {"id": "e7", "source": "n3", "target": "n6"},
                ],
            ),
        },
    ],
}


PRESET_EVENTO = {
    "id": "event",
    "name": "Event Wiki: Tech Conference",
    "description": "Event organisation, programme, speakers, logistics and volunteers",
    "articles": [
        (
            "Tech Conference 2025",
            "event-home",
            (
                "# Tech Conference 2025\n\n"
                "## 15–16 June 2025: Milan, Convention Centre\n\n"
                "Welcome to the organisational wiki for the **Tech Conference**! "
                "This platform contains all the information for organisers, "
                "speakers and volunteers.\n\n"
                "## Event figures\n\n"
                "| Fact | Value |\n"
                "|---|---|\n"
                "| Expected attendees | 350 |\n"
                "| Confirmed speakers | 18 |\n"
                "| Workshops | 6 |\n"
                "| Sponsors | 8 |\n"
                "| Volunteers | 15 |\n\n"
                "## Quick links\n\n"
                "- Full programme → page *Event Programme*\n"
                "- Logistics → page *Logistics and Venue*\n"
                "- Operational tasks → Kanban Board\n"
                "- Venue map → Canvas *Venue Floor Plan*"
            ),
        ),
        (
            "Event Programme",
            "event-programme",
            (
                "# Programme: Tech Conference 2025\n\n"
                "## Day 1: 15 June (Saturday)\n\n"
                "| Time | Main Hall | Workshop Room |\n"
                "|---|---|---|\n"
                "| 08:30–09:00 | Registration and welcome coffee | - |\n"
                "| 09:00–09:30 | **Opening**: Maria Esposito | - |\n"
                "| 09:30–10:15 | **Keynote:** \"The future of AI\": Prof. Rossi | - |\n"
                "| 10:15–10:30 | Coffee break | - |\n"
                "| 10:30–11:15 | \"Python for Data Science\": Luca Neri | Workshop: Docker Intro |\n"
                "| 11:15–12:00 | \"Cloud Native Security\": Sara Bianchi | Workshop: CI/CD with GitHub Actions |\n"
                "| 12:00–13:30 | Lunch break (networking) | - |\n"
                "| 13:30–14:15 | \"Open Source in business\": Marco Verdi | Workshop: Python Testing |\n"
                "| 14:15–15:00 | \"UX Design for developers\": Chiara Ricci | Workshop: Kubernetes basics |\n"
                "| 15:00–15:15 | Coffee break | - |\n"
                "| 15:15–16:00 | Panel: \"Tech start-ups\" | - |\n"
                "| 16:00–17:00 | Networking and demo area | - |\n\n"
                "## Day 2: 16 June (Sunday)\n\n"
                "| Time | Main Hall | Workshop Room |\n"
                "|---|---|---|\n"
                "| 09:00–09:45 | \"Machine Learning in production\": Dr. Ferrara | Workshop: API Design |\n"
                "| 09:45–10:30 | \"DevOps culture\": Paolo Romano | Workshop: Terraform |\n"
                "| 10:30–10:45 | Coffee break | - |\n"
                "| 10:45–11:30 | \"Web Accessibility\": Anna Greco | - |\n"
                "| 11:30–12:15 | **Closing Keynote:** \"Innovating responsibly\": Prof. Conti | - |\n"
                "| 12:15–12:30 | Closing remarks and next steps | - |"
            ),
        ),
        (
            "Logistics and Venue",
            "logistics-and-venue",
            (
                "# Logistics and Venue\n\n"
                "## Milan Convention Centre\n\n"
                "**Address:** Via Roma 42, 20121 Milan\n\n"
                "## How to get there\n\n"
                "- **Metro:** Line M1, stop *Cairoli* (5 min walk)\n"
                "- **Train:** Milan Central → Metro M1 (15 min)\n"
                "- **Car:** Affiliated car park *Garage Roma* (€10/day)\n\n"
                "## Rooms\n\n"
                "| Room | Capacity | Equipment |\n"
                "|---|---|---|\n"
                "| Main Hall | 400 seats | 4K projector, microphones, streaming |\n"
                "| Workshop Room | 60 seats | Projector, whiteboard, instructor PC |\n"
                "| Networking Area | 100 people | High tables, demo area |\n"
                "| Speaker Room | 15 seats | Dedicated Wi-Fi, printer |\n\n"
                "## Catering\n\n"
                "- Welcome coffee: 08:30\n"
                "- Morning coffee break: 10:15 and 10:30\n"
                "- Buffet lunch: 12:00–13:30\n"
                "- Afternoon coffee break: 15:00\n\n"
                "**Supplier:** Catering Milano Srl, contact: catering@example.com\n\n"
                "## Emergency contacts\n\n"
                "- Venue manager: Giorgio (extension 200)\n"
                "- Nearest hospital: Fatebenefratelli Hospital (1 km)"
            ),
        ),
        (
            "Speakers and Bios",
            "speakers-and-bios",
            (
                "# Speakers: Tech Conference 2025\n\n"
                "## Keynote Speakers\n\n"
                "### Prof. Alessandro Rossi\n"
                "**\"The future of AI\"**\n\n"
                "Full Professor of Artificial Intelligence at the Politecnico di Milano. "
                "Author of over 80 scientific publications, consultant to the "
                "European Commission on the AI Act.\n\n"
                "### Prof. Elena Conti\n"
                "**\"Innovating responsibly\"**\n\n"
                "Founder of EthicTech, a leading company in innovation ethics. "
                "TEDx speaker and member of the AI ethics committee.\n\n"
                "## Technical speakers\n\n"
                "| Name | Talk title | Company |\n"
                "|---|---|---|\n"
                "| Luca Neri | Python for Data Science | DataHub Srl |\n"
                "| Sara Bianchi | Cloud Native Security | CyberSec Italia |\n"
                "| Marco Verdi | Open Source in business | OpenIT Foundation |\n"
                "| Chiara Ricci | UX Design for developers | DesignLab |\n"
                "| Dr. Ferrara | Machine Learning in production | AIFactory |\n"
                "| Paolo Romano | DevOps culture | CloudOps |\n"
                "| Anna Greco | Web Accessibility | WebForAll |"
            ),
        ),
    ],
    "kanban": [
        {
            "title": "Event Organisation",
            "description": "Operational tasks for the preparation of Tech Conference 2025",
            "columns": [
                {
                    "title": "To Do",
                    "tickets": [
                        {"title": "Print attendee badges", "description": "350 badges with name, company and QR code: printer confirms by 10/06", "priority": "high"},
                        {"title": "Test audio/video equipment", "description": "Check projectors, microphones and streaming system in the main hall", "priority": "critical"},
                        {"title": "Prepare speaker kits", "description": "Bag with programme, HDMI/USB-C adapters, water bottle, VIP badge", "priority": "medium"},
                    ],
                },
                {
                    "title": "In Progress",
                    "tickets": [
                        {"title": "Confirm catering final menu", "description": "Awaiting confirmation for vegetarian and gluten-free options", "priority": "high"},
                        {"title": "Volunteer briefing", "description": "Organise Zoom call with the 15 volunteers for role assignment", "priority": "medium"},
                    ],
                },
                {
                    "title": "Pending",
                    "tickets": [
                        {"title": "Receive speaker slides", "description": "4 speakers still need to send their presentations: deadline 12/06", "priority": "high"},
                    ],
                },
                {
                    "title": "Done",
                    "tickets": [
                        {"title": "Venue booking", "description": "Milan Convention Centre confirmed for 15-16 June", "priority": "low"},
                        {"title": "Sponsor contracts", "description": "All 8 sponsors have signed the contract", "priority": "low"},
                        {"title": "Event website online", "description": "Landing page published on techconference2025.com", "priority": "low"},
                    ],
                },
            ],
        },
    ],
    "canvases": [
        {
            "title": "Venue Floor Plan",
            "slug": "venue-floor-plan",
            "description": "Layout of the rooms and areas of the event",
            "data": _canvas_data(
                nodes=[
                    {"id": "n1", "type": "sticky", "position": {"x": 250, "y": 50}, "data": {"label": "ENTRANCE\nRegistration", "color": "#fbbf24"}},
                    {"id": "n2", "type": "sticky", "position": {"x": 100, "y": 200}, "data": {"label": "Main Hall\n400 seats", "color": "#60a5fa"}},
                    {"id": "n3", "type": "sticky", "position": {"x": 400, "y": 200}, "data": {"label": "Workshop Room\n60 seats", "color": "#60a5fa"}},
                    {"id": "n4", "type": "sticky", "position": {"x": 250, "y": 350}, "data": {"label": "Networking Area\nDemo + Sponsors", "color": "#34d399"}},
                    {"id": "n5", "type": "sticky", "position": {"x": 500, "y": 350}, "data": {"label": "Speaker Room\nBackstage", "color": "#f87171"}},
                    {"id": "n6", "type": "sticky", "position": {"x": 50, "y": 350}, "data": {"label": "Catering Area\nCoffee + Lunch", "color": "#a78bfa"}},
                ],
                edges=[
                    {"id": "e1", "source": "n1", "target": "n2"},
                    {"id": "e2", "source": "n1", "target": "n3"},
                    {"id": "e3", "source": "n1", "target": "n4"},
                    {"id": "e4", "source": "n2", "target": "n5"},
                    {"id": "e5", "source": "n4", "target": "n6"},
                ],
            ),
        },
    ],
}


PRESET_SCUOLA = {
    "id": "school",
    "name": "School / Institute Wiki",
    "description": "School management: teachers, classes, curricula and communications",
    "articles": [
        (
            "Institute Portal: Home",
            "school-home",
            (
                "# Wiki Portal: State Technical Institute\n\n"
                "Welcome to the institute's wiki portal. This platform "
                "is the reference point for **teachers**, **administrative staff** "
                "and **students**.\n\n"
                "## Main sections\n\n"
                "- **Educational Offer Plan**: curriculum, programmes and objectives\n"
                "- **Class Timetables**: weekly schedule per class\n"
                "- **Circulars**: communications from management\n"
                "- **School Regulations**: rules of conduct\n"
                "- **Projects**: extracurricular activities and work experience\n\n"
                "## Contacts\n\n"
                "| Office | Contact | Email |\n"
                "|---|---|---|\n"
                "| Management | Prof. Martini | headteacher@example.edu |\n"
                "| Academic Secretary | Ms Conti | secretary@example.edu |\n"
                "| Vice Principal | Prof. Lombardi | vlombardi@example.edu |\n"
                "| Guidance | Prof. Ferrara | guidance@example.edu |"
            ),
        ),
        (
            "Class Timetables: First Year",
            "timetables-first-year",
            (
                "# Class Timetables: First Year\n\n"
                "## 1A: Computer Science track\n\n"
                "| Period | Monday | Tuesday | Wednesday | Thursday | Friday |\n"
                "|---|---|---|---|---|---|\n"
                "| 1st (8:00) | Italian | Mathematics | English | Computer Science | Italian |\n"
                "| 2nd (9:00) | Italian | Mathematics | English | Computer Science | History |\n"
                "| 3rd (10:00) | Mathematics | Science | Italian | Physics | PE |\n"
                "| 4th (11:00) | English | Computer Science | Mathematics | Physics | PE |\n"
                "| 5th (12:00) | History | Computer Science | Science | Law | - |\n\n"
                "## 1B: Electronics track\n\n"
                "| Period | Monday | Tuesday | Wednesday | Thursday | Friday |\n"
                "|---|---|---|---|---|---|\n"
                "| 1st (8:00) | Mathematics | Italian | Electronics | English | Mathematics |\n"
                "| 2nd (9:00) | Mathematics | Italian | Electronics | English | Science |\n"
                "| 3rd (10:00) | English | Physics | Italian | Mathematics | Electronics |\n"
                "| 4th (11:00) | Science | Physics | History | PE | Electronics |\n"
                "| 5th (12:00) | Law | Electronics | History | PE | - |\n\n"
                "**Note:** The last period on Friday is free for catch-up activities."
            ),
        ),
        (
            "School Regulations",
            "school-regulations",
            (
                "# School Regulations\n\n"
                "## Timetable\n\n"
                "- Entry: **07:50–08:00** (first bell)\n"
                "- Lessons: **08:00–13:00** (5 periods of 60 minutes)\n"
                "- Break: **10:50–11:05**\n"
                "- Exit: **13:00** (or 14:00 for afternoon sessions)\n\n"
                "## Absences and late arrivals\n\n"
                "- Absences must be justified within **3 days**\n"
                "- After **5 late arrivals** in the term: parents summoned\n"
                "- Over **25% absences** in the term: risk of failing the year\n\n"
                "## Behaviour\n\n"
                "1. Students must bring their personal record book every day\n"
                "2. Mobile phones must be switched off during lessons\n"
                "3. Leaving the institute during school hours is not permitted\n"
                "4. Mutual respect between students and teachers is essential\n\n"
                "## Disciplinary measures\n\n"
                "| Infringement | Measure |\n"
                "|---|---|\n"
                "| Repeated lateness | Note in the register |\n"
                "| Mobile phone use in class | Device confiscated until end of day |\n"
                "| Misconduct | Disciplinary note + family summoned |\n"
                "| Damage to facilities | Compensation + 1–3 day suspension |"
            ),
        ),
        (
            "Work Experience and Extracurricular Activities",
            "work-experience-projects",
            (
                "# Work Experience and Extracurricular Activities\n\n"
                "## Work Experience (FSL)\n\n"
                "### Project \"Coding in Business\"\n"
                "- **Partner:** SoftwareItalia Srl\n"
                "- **Classes:** 3A, 3B, 4A\n"
                "- **Hours:** 80 hours (40 on-site + 40 online)\n"
                "- **Period:** February–May 2025\n"
                "- **Company mentor:** Eng. Martini\n"
                "- **School tutor:** Prof. De Rosa\n\n"
                "### Project \"Educational Robotics\"\n"
                "- **Partner:** FabLab Milano\n"
                "- **Classes:** 2A, 2B\n"
                "- **Hours:** 40 hours in the lab\n"
                "- **Period:** March–April 2025\n\n"
                "## Extracurricular activities\n\n"
                "| Activity | Day | Time | Coordinator |\n"
                "|---|---|---|---|\n"
                "| Robotics lab | Tuesday | 14:00–16:00 | Prof. Neri |\n"
                "| Theatre group | Wednesday | 14:30–16:30 | Prof. Ricci |\n"
                "| B2 certification course | Thursday | 14:00–15:30 | Prof. Smith |\n"
                "| School newspaper | Friday | 13:00–14:30 | Prof. Bianchi |"
            ),
        ),
    ],
    "kanban": [
        {
            "title": "Institute Activities: Term 2",
            "description": "Planning of school activities for the second term",
            "columns": [
                {
                    "title": "To Plan",
                    "tickets": [
                        {"title": "Third-year school trip", "description": "Organise guided visit to the Science Museum: quotes from 3 agencies", "priority": "medium"},
                        {"title": "University guidance day", "description": "Contact universities and ITS for presentations to fifth-year students", "priority": "high"},
                    ],
                },
                {
                    "title": "In Preparation",
                    "tickets": [
                        {"title": "May class council meetings", "description": "Convene all class councils by 20 May: prepare minutes", "priority": "high"},
                        {"title": "INVALSI tests for second years", "description": "Check computer lab availability for 12-15 May dates", "priority": "critical"},
                    ],
                },
                {
                    "title": "In Progress",
                    "tickets": [
                        {"title": "First term grades", "description": "Enter grades in the electronic register: deadline 31 May", "priority": "critical"},
                    ],
                },
                {
                    "title": "Completed",
                    "tickets": [
                        {"title": "Open Day 2025", "description": "Successful event with 120 participating families: report published", "priority": "low"},
                        {"title": "Health and safety training", "description": "All teachers have completed the mandatory training course", "priority": "low"},
                        {"title": "Purchase new lab PCs", "description": "15 new workstations installed in Lab C: testing completed", "priority": "medium"},
                    ],
                },
            ],
        },
    ],
    "canvases": [
        {
            "title": "Educational Offer Map",
            "slug": "educational-offer",
            "description": "Study programmes and educational pathways at the institute",
            "data": _canvas_data(
                nodes=[
                    {"id": "n1", "type": "sticky", "position": {"x": 280, "y": 50}, "data": {"label": "State Technical\nInstitute", "color": "#fbbf24"}},
                    {"id": "n2", "type": "sticky", "position": {"x": 80, "y": 200}, "data": {"label": "Computer Science\nand Telecoms", "color": "#60a5fa"}},
                    {"id": "n3", "type": "sticky", "position": {"x": 280, "y": 200}, "data": {"label": "Electronics\nand Electrical Eng.", "color": "#60a5fa"}},
                    {"id": "n4", "type": "sticky", "position": {"x": 480, "y": 200}, "data": {"label": "Business\nFinance and Marketing", "color": "#60a5fa"}},
                    {"id": "n5", "type": "sticky", "position": {"x": 80, "y": 370}, "data": {"label": "Work Experience\nInternships", "color": "#34d399"}},
                    {"id": "n6", "type": "sticky", "position": {"x": 280, "y": 370}, "data": {"label": "Certifications\nCisco, ECDL, B2", "color": "#34d399"}},
                    {"id": "n7", "type": "sticky", "position": {"x": 480, "y": 370}, "data": {"label": "Pathways\nUniversity and ITS", "color": "#a78bfa"}},
                ],
                edges=[
                    {"id": "e1", "source": "n1", "target": "n2"},
                    {"id": "e2", "source": "n1", "target": "n3"},
                    {"id": "e3", "source": "n1", "target": "n4"},
                    {"id": "e4", "source": "n2", "target": "n5"},
                    {"id": "e5", "source": "n3", "target": "n5"},
                    {"id": "e6", "source": "n2", "target": "n6"},
                    {"id": "e7", "source": "n3", "target": "n6"},
                    {"id": "e8", "source": "n4", "target": "n6"},
                    {"id": "e9", "source": "n2", "target": "n7"},
                    {"id": "e10", "source": "n3", "target": "n7"},
                    {"id": "e11", "source": "n4", "target": "n7"},
                ],
            ),
        },
    ],
}


PRESET_COMUNE_BOLZANO_PUBBLICO = {
    "id": "comune-bolzano-pubblico",
    "name": "Comune di Bolzano: Wiki Pubblica",
    "description": "Portale cittadino: servizi, albo pretorio, urbanistica, eventi e trasporti",
    "articles": [
        (
            "Benvenuto: Comune di Bolzano",
            "bolzano-benvenuto",
            (
                "# Benvenuto nel portale wiki del Comune di Bolzano\n\n"
                "Questa wiki è il punto di riferimento per i cittadini di Bolzano. "
                "Qui troverai informazioni sui servizi comunali, bandi, eventi, "
                "urbanistica, trasporti e molto altro.\n\n"
                "## Sezioni principali\n\n"
                "- **Servizi al cittadino**: anagrafe, stato civile, tributi\n"
                "- **Albo Pretorio**: avvisi ufficiali e bandi di gara\n"
                "- **Urbanistica e Territorio**: piani, permessi, vincoli\n"
                "- **Trasporti e Mobilità**: parcheggi, linee bus, ZTL\n"
                "- **Eventi e Cultura**: manifestazioni, musei, biblioteche\n"
                "- **Ambiente e Ecologia**: raccolta rifiuti, verde pubblico\n\n"
                "## Contatti\n\n"
                "| Ufficio | Telefono | Email |\n"
                "|---|---|---|\n"
                "| Segreteria Generale | 0471-111111 | segreteria@comune.example.it |\n"
                "| Ufficio Anagrafe | 0471-111112 | anagrafe@comune.example.it |\n"
                "| Edilizia Privata | 0471-111113 | edilizia@comune.example.it |\n"
                "| Tributi | 0471-111114 | tributi@comune.example.it |\n\n"
                "---\n\n"
                "*Ultimo aggiornamento: mese di giugno 2026*"
            ),
        ),
        (
            "Servizi al Cittadino",
            "bolzano-servizi",
            (
                "# Servizi al Cittadino\n\n"
                "## Anagrafe\n\n"
                "| Servizio | Dove | Orario |\n"
                "|---|---|---|\n"
                "| Iscrizione anagrafica | Palazzo Civico, Via Portici 30 | Lun-Ven 8:30-12:30 |\n"
                "| Carta d'identità | Palazzo Civico | Lun-Ven 8:30-12:30 |\n"
                "| Passaporto | Palazzo Civico | Solo su appuntamento |\n"
                "| Stato di famiglia | Palazzo Civico | Lun-Ven 8:30-12:30 |\n\n"
                "## Stato Civile\n\n"
                "- Matrimoni civili e concordatari\n"
                "- Unioni civili\n"
                "- Riconoscimenti e adozioni\n"
                "- Certificati di morte, nascita e matrimonio\n\n"
                "## Tributi Locali\n\n"
                "| Imposta | Scadenza |\n"
                "|---|---|\n"
                "| TARI (rifiuti) | 30 giugno e 31 dicembre |\n"
                "| IMU | 16 giugno e 16 dicembre |\n"
                "| Bollo auto | Alla scadenza del veicolo |\n\n"
                "Pagamenti online: [pagamenti.comune.example.it](https://example.com)\n\n"
                "## Documenti e Certificati\n\n"
                "La maggior parte dei certificati può essere richiesta online tramite "
                "SPID o CIE. I certificati doganali e di conformità sono disponibili "
                "all'ufficio competente."
            ),
        ),
        (
            "Albo Pretorio: Bandi e Avvisi",
            "bolzano-albo-pretorio",
            (
                "# Albo Pretorio: Bandi e Avvisi Ufficiali\n\n"
                "L'Albo Pretorio è il registro ufficiale degli atti del Comune. "
                "Gli avvisi restano pubblicati per **60 giorni** salvo diversa indicazione.\n\n"
                "## Bandi di gara attivi\n\n"
                "| Bando | Oggetto | Scadenza |\n"
                "|---|---|---|\n"
                "| BG-2026-041 | Manutenzione verde pubblico I semestre | 15/07/2026 |\n"
                "| BG-2026-042 | Fornitura materiali uffici comunali | 20/07/2026 |\n"
                "| BG-2026-043 | Servizio pulizia scuole | 25/07/2026 |\n\n"
                "## Avvisi pubblici\n\n"
                "- **10/06/2026**, Varco ZTL Piazza Walther: chiusura notturna dal 15 giugno\n"
                "- **08/06/2026**: Apertura bando assegnazione locali commerciali via dei Portici\n"
                "- **05/06/2026**, Conferenza pubblica sul Piano della Mobilità, 20 giugno ore 18:00\n\n"
                "## Come partecipare\n\n"
                "1. Consultare il bando nella sezione corretta\n"
                "2. Scaricare il disciplinare e gli allegati\n"
                "3. Compilare la documentazione richiesta\n"
                "4. Inviare la domanda nei termini indicati\n"
                "5. Consultare l'esito nella sezione esiti"
            ),
        ),
        (
            "Urbanistica e Territorio",
            "bolzano-urbanistica",
            (
                "# Urbanistica e Territorio\n\n"
                "## Piani e Regolamenti\n\n"
                "| Piano | Stato |\n"
                "|---|---|\n"
                "| Piano Strutturale Comunale (PSC) | Approvato |\n"
                "| Piano Operativo Comunale (POC) | In aggiornamento |\n"
                "| Regolamento Edilizio | Vigente |\n"
                "| Piano del Verde | In revisione |\n\n"
                "## Permessi di Costruire\n\n"
                "### Dove presentare\n"
                "Ufficio Edilizia Privata: Palazzo Civico, Via Portici 30\n\n"
                "### Documenti necessari\n"
                "- Domanda al Protocollo Unico Edilizia (PUE)\n"
                "- Progetto esecutivo con relazione tecnica\n"
                "- Titoli di proprietà o altro titolo abilitativo\n"
                "- Dichiarazione di conformità urbanistica e catastale\n"
                "- Segnalazione certificata di inizio attività (SCIA) per interventi minori\n\n"
                "### Tempi di lavorazione\n\n"
                "| Procedura | Termine |\n"
                "|---|---|\n"
                "| SCIA | 30 giorni |\n"
                "| Permesso di costruire | 90 giorni |\n"
                "| Concessione sanatoria | 60 giorni |\n\n"
                "## Vincoli e Tutela\n\n"
                "- Vincolo paesaggistico (D.Lgs. 42/2004)\n"
                "- Vincolo idrogeologico (D.M. 327/2003)\n"
                "- Vincolo storico-artistico (D.Lgs. 42/2004)\n"
                "- Area di protezione del paesaggio"
            ),
        ),
        (
            "Trasporti e Mobilità",
            "bolzano-trasporti",
            (
                "# Trasporti e Mobilità\n\n"
                "## Rete di Mobilità Integrata (RHI)\n\n"
                "| Linea | Percorso | Frequenza |\n"
                "|---|---|---|\n"
                "| Linea 1 | Bolzano FS, Gries, San Quirino | Ogni 10 min |\n"
                "| Linea 2 | Bolzano FS, Don Bosco, Irnerio | Ogni 12 min |\n"
                "| Linea 3 | Bolzano FS: Zona Industriale | Ogni 15 min |\n"
                "| Linea 4 | Bolzano FS, Carducci, Ora | Ogni 15 min |\n"
                "| Linea 5 | Bolzano FS, Appiano, Caldaro | Ogni 20 min |\n\n"
                "### Biglietti\n\n"
                "| Tipo | Prezzo |\n"
                "|---|---|\n"
                "| Biglietto singolo (zona 100) | EUR 1,50 |\n"
                "| Abbonamento mensile | EUR 42,00 |\n"
                "| Abbonamento annuale | EUR 380,00 |\n"
                "| Biglietto giornaliero | EUR 4,00 |\n\n"
                "## Parcheggi\n\n"
                "- **Parcheggi blu**, a pagamento Lun-Sab 8:00-19:00\n"
                "- **Parcheggi residenti**: abbonamento annuale EUR 120\n"
                "- **Parcheggi a scomparsa**: free parking a rotazione\n"
                "- **P+R**: parcheggi di interscambio alle fermate della SAD\n\n"
                "## ZTL: Zona a Traffico Limitato\n\n"
                "Le aree pedonali del centro storico sono soggette a restrizioni:\n"
                "- **Lun-Ven**: accesso solo residenti e autorizzati 7:00-19:00\n"
                "- **Sab-Dom**: libero accesso pedonale\n"
                "- Violazioni: sanzione EUR 80 + rimozione forzata"
            ),
        ),
        (
            "Eventi e Cultura",
            "bolzano-eventi-cultura",
            (
                "# Eventi e Cultura\n\n"
                "## Manifestazioni in programma\n\n"
                "| Evento | Luogo | Data |\n"
                "|---|---|---|\n"
                "| Bolzano Jazz Festival | Piazza Walther | 5-8 luglio 2026 |\n"
                "| Mercatini di Natale | Centro storico | 24 nov - 6 gen 2026 |\n"
                "| Festival dell'Architettura | Vari luoghi | 15-22 settembre 2026 |\n"
                "| Corso di cucina ladina | Palais congiuntamente | 10 agosto 2026 |\n\n"
                "## Musei e Biblioteche\n\n"
                "| Struttura | Indirizzo | Orario |\n"
                "|---|---|---|\n"
                "| Museo Civico | Via Museo 15 | Mar-Dom 10-18 |\n"
                "| Museo Archeologico | Via Museo 43 | Mar-Dom 10-18 |\n"
                "| Biblioteca Civica | Via dei Bottai 36 | Lun-Ven 9-20, Sab 9-13 |\n"
                "| Galleria Civica d'Arte Moderna | Via Dante 15 | Mar-Dom 10-18 |\n\n"
                "## Contributi e Finanziamenti\n\n"
                "Il Comune bandisce annualmente contributi per:\n"
                "- Iniziative culturali e artistiche\n"
                "- Associazioni di promozione sociale\n"
                "- Progetti di integrazione e multilinguismo\n"
                "- Attività sportive e ricreative\n\n"
                "I bandi vengono pubblicati nell'Albo Pretorio."
            ),
        ),
        (
            "Ambiente e Ecologia",
            "bolzano-ambiente",
            (
                "# Ambiente e Ecologia\n\n"
                "## Raccolta Rifiuti\n\n"
                "| Rifiuto | Freq. | Giorno |\n"
                "|---|---|---|\n"
                "| Umido | Settimanale | Sabato |\n"
                "| Secco | Quindicinale | Mercoledì alterni |\n"
                "| Carta | Settimanale | Venerdì |\n"
                "| Plastica | Settimanale | Giovedì |\n"
                "| Vetro | Mensile | 1° sabato del mese |\n"
                "| Indifferenziato | Quindicinale | Martedì alterni |\n\n"
                "## Contenitori e Ritiro\n\n"
                "- Contenitori stradali per carta, plastica, vetro e indifferenziato\n"
                "- Isole ecologiche per rifiuti ingombranti (su prenotazione)\n"
                "- Centro raccolta via dei Portici per RAEE e oli esausti\n\n"
                "## Verde Pubblico\n\n"
                "- Manutenzione parchchi pubblici: aprile-ottobre\n"
                "- Piantumazione alberi: marzo-novembre\n"
                "- Manutenzione fioriere: aprile-ottobre\n\n"
                "## Qualità dell'Aria\n\n"
                "| Indicatore | Valore limite |\n"
                "|---|---|\n"
                "| PM10 | 50 µg/m³ (media giornaliera) |\n"
                "| PM2.5 | 25 µg/m³ (media annuale) |\n"
                "| NO₂ | 200 µg/m³ (media oraria) |\n\n"
                "Monitoraggio in tempo reale: [aria.provincia.bz.it](https://example.com)"
            ),
        ),
    ],
    "kanban": [
        {
            "title": "Lavori Pubblici: Cantiere 2026",
            "description": "Stato avanzamento dei principali interventi sul territorio comunale",
            "columns": [
                {
                    "title": "Da Avviare",
                    "tickets": [
                        {"title": "Ristrutturazione Piazza dei Battuti", "description": "Progetto definitivo approvato, attesa assegnazione appalto", "priority": "medium"},
                        {"title": "Sostituzione illuminate Via Stafeni", "description": "Passaggio a LED, risparmio energetico stimato 40%", "priority": "low"},
                    ],
                },
                {
                    "title": "In Corso",
                    "tickets": [
                        {"title": "Lavori strada del Sale", "description": "Pavimentazione in porfido, completamento previsto fine luglio", "priority": "high"},
                        {"title": "Impianto fotovoltaico palazzo comunale", "description": "Installazione pannelli sul tetto, produzione 120 kWp", "priority": "high"},
                        {"title": "Percorso ciclabile Bolzano-Appiano", "description": "Tratto 3 su 5 completato, in corso completamento tratto 4", "priority": "medium"},
                    ],
                },
                {
                    "title": "In ConsegnA",
                    "tickets": [
                        {"title": "Parco giochi Don Bosco", "description": "Nuovi giochi inclusivi installati, collaudo effettuato", "priority": "low"},
                    ],
                },
                {
                    "title": "Completati",
                    "tickets": [
                        {"title": "Riqualificazione via Argentieri", "description": "Lavori completati, inaugurazione 1 giugno 2026", "priority": "low"},
                        {"title": "Sistema di irrigazione parchco Talvera", "description": "Impianto automatico con controllo remoto", "priority": "low"},
                    ],
                },
            ],
        },
    ],
    "canvases": [
        {
            "title": "Mappa dei Servizi Communali",
            "slug": "mappa-servizi",
            "description": "Distribuzione degli uffici e servizi sul territorio comunale",
            "data": _canvas_data(
                nodes=[
                    {"id": "n1", "type": "sticky", "position": {"x": 300, "y": 50}, "data": {"label": "Palazzo Civico\nVia Portici 30\nUffici Generali", "color": "#fbbf24"}},
                    {"id": "n2", "type": "sticky", "position": {"x": 100, "y": 200}, "data": {"label": "Anagrafe e Stato Civile\nPalazzo Civico", "color": "#60a5fa"}},
                    {"id": "n3", "type": "sticky", "position": {"x": 300, "y": 200}, "data": {"label": "Edilizia Privata\nVia Portici 30", "color": "#60a5fa"}},
                    {"id": "n4", "type": "sticky", "position": {"x": 500, "y": 200}, "data": {"label": "Tributi\nVia Argentieri 18", "color": "#60a5fa"}},
                    {"id": "n5", "type": "sticky", "position": {"x": 100, "y": 370}, "data": {"label": "Museo Civico\nVia Museo 15", "color": "#34d399"}},
                    {"id": "n6", "type": "sticky", "position": {"x": 300, "y": 370}, "data": {"label": "Biblioteca Civica\nVia dei Bottai 36", "color": "#34d399"}},
                    {"id": "n7", "type": "sticky", "position": {"x": 500, "y": 370}, "data": {"label": "Centro Servizi\nVia Brennero 6", "color": "#a78bfa"}},
                ],
                edges=[
                    {"id": "e1", "source": "n1", "target": "n2"},
                    {"id": "e2", "source": "n1", "target": "n3"},
                    {"id": "e3", "source": "n1", "target": "n4"},
                    {"id": "e4", "source": "n1", "target": "n7"},
                    {"id": "e5", "source": "n1", "target": "n5"},
                    {"id": "e6", "source": "n1", "target": "n6"},
                ],
            ),
        },
    ],
}


PRESET_COMUNE_BOLZANO_INTERNO = {
    "id": "comune-bolzano-interno",
    "name": "Comune di Bolzano: Wiki Interna",
    "description": "Knowledge base interna: procedure, HR, IT, comunicazione e governance",
    "articles": [
        (
            "Benvenuto: Wiki Interna Comune di Bolzano",
            "bolzano-interno-benvenuto",
            (
                "# Benvenuto nella Wiki Interna del Comune di Bolzano\n\n"
                "Questa wiki è riservata al personale del Comune. Contiene procedure "
                "operative, linee guida, documentazione interna e strumenti di lavoro.\n\n"
                "## Sezioni principali\n\n"
                "- **Procedure Operative**: SOP per ogni settore\n"
                "- **Risorse Umane**: politiche, contratti, formazione\n"
                "- **IT e Sistemi**: infrastruttura, sicurezza, software\n"
                "- **Comunicazione Interna**: circolari, verbali, note\n"
                "- **Governance**: organigramma, deleghe, delibere\n\n"
                "## Regole d'uso\n\n"
                "1. Non condividere contenuti sensibili al di fuori della wiki\n"
                "2. Aggiornare le pagine quando le procedure cambiano\n"
                "3. Segnalare contenuti obsoleti all'ufficio competente\n"
                "4. Usare la chat interna per discussioni, le pagine per conoscenza duratura\n\n"
                "---\\n\n"
                "*Accesso riservato al personale comunale autorizzato.*"
            ),
        ),
        (
            "Organigramma e Deleghe",
            "bolzano-interno-organigramma",
            (
                "# Organigramma e Deleghe\n\n"
                "## Struttura organizzativa\n\n"
                "| Dirigenza | Settore | Responsabile |\n"
                "|---|---|---|\n"
                "| Segretario Generale | Segreteria, Protocollo, Personale | Dott. M. Rossi |\n"
                "| Dir. Urbanistica | Edilizia, Lavori Pubblici, Territorio | Ing. L. Bianchi |\n"
                "| Dir. Economia | Tributi, Bilancio, Patrimonio | Dott.ssa A. Verdi |\n"
                "| Dir. Servizi | Anagrafe, Stato Civile, Sociale | Dott. G. Neri |\n"
                "| Dir. Ambiente | Ecologia, Verde, Rifiuti, Mobilità | Dott.ssa S. Fontana |\n\n"
                "## Deleghe del Sindaco\n\n"
                "| Delega | Assessor/a | Fino al |\n"
                "|---|---|---|\n"
                "| Lavori Pubblici | Ass. P. Conti | 30/06/2027 |\n"
                "| Cultura e Turismo | Ass. F. Lombardi | 30/06/2027 |\n"
                "| Ambiente e Mobilità | Ass. R. Esposito | 30/06/2027 |\n"
                "| Politiche Sociali | Ass. C. Romano | 30/06/2027 |\n\n"
                "## Catena di comando\n\n"
                "1. Funzionario di turno → Responsabile di settore\n"
                "2. Responsabile di settore → Dirigente\n"
                "3. Dirigente → Sindaco (per decisioni politiche)\n"
                "4. In caso di urgenza: contattare direttamente il Dirigente di riferimento"
            ),
        ),
        (
            "Procedure di Acquisto e Gare",
            "bolzano-interno-gare",
            (
                "# Procedure di Acquisto e Gare\n\n"
                "## Soglie di affidamento\n\n"
                "| Importo | Procedura |\n"
                "|---|---|\n"
                "| Fino a EUR 5.000 | Acquisto diretto (3 preventivi) |\n"
                "| EUR 5.000 - 40.000 | Procedura negoziata (5 preventivi) |\n"
                "| Oltre EUR 40.000 | Gara pubblica (bandito aperto) |\n\n"
                "## Checklist acquisto diretto\n\n"
                "- [ ] Verificare disponibilità fondo spesa\n"
                "- [ ] Richiedere almeno 3 preventivi\n"
                "- [ ] Confrontare offerte e selezionare la più vantaggiosa\n"
                "- [ ] Compilare modulo di acquisto diretto\n"
                "- [ ] Allegare preventivi e verbale\n"
                "- [ ] Inviare al Responsabile del Servizio per approvazione\n\n"
                "## Tempi di lavorazione\n\n"
                "| Fase | Termine |\n"
                "|---|---|\n"
                "| Valutazione preventiva | 5 giorni lavorativi |\n"
                "| Approvazione Dirigente | 3 giorni lavorativi |\n"
                "| Ordine fornitore | 2 giorni lavorativi |\n"
                "| Consegna merce | 15-30 giorni (a seconda del tipo) |\n\n"
                "## Documentazione da conservare\n\n"
                "Tutta la documentazione di gara deve essere archiviata nel sistema "
                "di gestione documentale e conservata per **10 anni** (art. 2222 c.c.)."
            ),
        ),
        (
            "Gestione del Personale",
            "bolzano-interno-personale",
            (
                "# Gestione del Personale\n\n"
                "## Assenze e Ferie\n\n"
                "| Tipo assenza | Richiesta | Approvazione |\n"
                "|---|---|---|\n"
                "| Ferie | Almeno 15 giorni prima | Responsabile settore |\n"
                "| Malattia | Entro 48 ore (certificato medico) | Automatica |\n"
                "| Permesso retribuito | Almeno 3 giorni prima | Responsabile settore |\n"
                "| Aspettativa | Almeno 30 giorni prima | Dirigente |\n\n"
                "## Formazione Obbligatoria\n\n"
                "- Sicurezza sul lavoro: **4 ore/anno** (D.Lgs. 81/2008)\n"
                "- Anticorruzione: **2 ore/anno** (L. 212/2000)\n"
                "- Privacy e protezione dati: **2 ore/anno** (GDPR)\n"
                "- Antimaltrattamento: **1 ora/anno** (L. 104/1990)\n\n"
                "## Valutazione delle Prestazioni\n\n"
                "Il ciclo di valutazione annuale prevede:\n"
                "1. Autovalutazione del dipendente (gennaio)\n"
                "2. Colloquio con il responsabile (febbraio)\n"
                "3. Definizione obiettivi (marzo)\n"
                "4. Monitoraggio semestrale (luglio)\n"
                "5. Valutazione finale (dicembre)\n\n"
                "## Carriere e Mobilità\n\n"
                "- Progressioni di carriera: bando interno annualmente\n"
                "- Mobilità tra settori: richiesta al Dirigente + HR\n"
                "- Concorsi pubblici: pubblicati su Albo Pretorio e Gazzetta Ufficiale"
            ),
        ),
        (
            "IT e Sicurezza Informatica",
            "bolzano-interno-it",
            (
                "# IT e Sicurezza Informatica\n\n"
                "## Infrastruttura di base\n\n"
                "| Sistema | Tecnologia |\n"
                "|---|---|\n"
                "| Email | Microsoft 365 / Exchange |\n"
                "| Directory | Active Directory + Azure AD |\n"
                "| VPN | WireGuard (accesso da remoto) |\n"
                "| Backup | Veeam + NAS locale |\n"
                "| Firewall | FortiGate |\n\n"
                "## Regole di sicurezza\n\n"
                "1. **Password**: minimo 12 caratteri, cambio ogni 90 giorni\n"
                "2. **2FA**: obbligatoria su tutti gli account\n"
                "3. **VPN**: obbligatoria per accesso da rete esterna\n"
                "4. **USB**: vietate pen drive non autorizzate\n"
                "5. **Email**: non aprire allegati da mittenti sconosciuti\n\n"
                "## Procedure di emergenza\n\n"
                "| Incidente | Azione |\n"
                "|---|---|\n"
                "| Furto notebook | Contattare IT entro 1 ora, disabilitare account |\n"
                "| Phishing ricevuto | Non cliccare link, segnalare a phishing@comune.example.it |\n"
                "| Ransomware | Scollegarsi dalla rete, chiamare IT immediatamente |\n"
                "| Perdita dati | Contattare IT per ripristino backup |\n\n"
                "## Software autorizzati\n\n"
                "- Microsoft Office 365\n"
                "- LibreOffice (alternativa)\n"
                "- Firefox / Chrome (browser)\n"
                "- Adobe Acrobat Reader\n"
                "- TeamViewer (solo con autorizzazione IT)"
            ),
        ),
        (
            "Comunicazione Interna e Circolari",
            "bolzano-interno-comunicazioni",
            (
                "# Comunicazione Interna e Circolari\n\n"
                "## Tipologie di comunicazione\n\n"
                "| Tipo | Destinatari | Canale |\n"
                "|---|---|---|\n"
                "| Circolare generale | Tutti i dipendenti | Email + affissione |\n"
                "| Nota di servizio | Settore specifico | Email interna |\n"
                "| Verbale di riunione | Partecipanti | Email + wiki |\n"
                "| Delibera di giunta | Dirigenti | Protocollo |\n"
                "| Avviso urgenza | Tutti | SMS + Email |\n\n"
                "## Template circolare\n\n"
                "```\n"
                "CIRCOLARE N. [numero]/[anno]\n"
                "OGGETTO: [oggetto]\n"
                "DATA: [data]\n"
                "DA: [mittente]\n"
                "A: [destinatari]\n\n"
                "[corpo del testo]\n\n"
                "Il/la [nome e cognome]\n"
                "[ruolo]\n"
                "```\n\n"
                "## Registrazione\n\n"
                "Tutte le circolari devono essere:\n"
                "1. Registrate nel sistema di protocollo\n"
                "2. Archiviate nel cartella del settore competente\n"
                "3. Pubblicate nella wiki interna\n"
                "4. Conservate per **5 anni** (circolari) o **20 anni** (delibere)"
            ),
        ),
        (
            "Protocollo e Gestione Documentale",
            "bolzano-interno-protocollo",
            (
                "# Protocollo e Gestione Documentale\n\n"
                "## Numerazione protocollo\n\n"
                "Formato: `[ANNO]/[CODICE SETTORE]/[NUMERO PROGRESSIVO]`\n\n"
                "Esempio: `2026/SGR/00142`\n\n"
                "## Tempi di protocollazione\n\n"
                "| Tipo documento | Termine |\n"
                "|---|---|\n"
                "| Corrispondenza in arrivo | Entro 24 ore |\n"
                "| Corrispondenza in uscita | Entro 48 ore |\n"
                "| PEC | Entro 12 ore |\n"
                "| Ordinanze | Entro 24 ore |\n\n"
                "## Classificazione\n\n"
                "Usare la tassonomia comunale:\n"
                "- **SGR**: Segreteria Generale\n"
                "- **URB**: Urbanistica\n"
                "- **ECO**: Economia\n"
                "- **SER**: Servizi\n"
                "- **AMB**: Ambiente\n"
                "- **POL**: Polizia Locale\n\n"
                "## Conservazione\n\n"
                "| Tipo | Durata |\n"
                "|---|---|\n"
                "| Documenti amministrativi | 10 anni |\n"
                "| Documenti contabili | 10 anni |\n"
                "| Delibere di giunta | 20 anni |\n"
                "| Delibere di consiglio | Permanente |\n"
                "| Piani urbanistici | Permanente |"
            ),
        ),
    ],
    "kanban": [
        {
            "title": "Sprint Operativo: Q3 2026",
            "description": "Attività operative prioritarie per il terzo trimestre",
            "columns": [
                {
                    "title": "Da Fare",
                    "tickets": [
                        {"title": "Aggiornamento regolamento edilizio", "description": "Revisione del regolamento edilizio comunale in conformità al POC aggiornato", "priority": "high"},
                        {"title": "Formazione sicurezza lavoro, turno 3", "description": "Organizzare corsi obbligatori per 120 dipendenti entro settembre", "priority": "medium"},
                        {"title": "Inventario patrimonio comunale", "description": "Aggiornamento inventario beni mobili e immobili per bilancio 2027", "priority": "medium"},
                    ],
                },
                {
                    "title": "In Corso",
                    "tickets": [
                        {"title": "Migrazione Exchange → Microsoft 365", "description": "Fase 2 di 3: migrazione caselle email, completamento previsto fine luglio", "priority": "critical"},
                        {"title": "Piano della Mobilità comunale", "description": "Raccolta dati e consultazione pubblica in corso, scadenza 30 settembre", "priority": "high"},
                    ],
                },
                {
                    "title": "In Revisione",
                    "tickets": [
                        {"title": "Nuovo regolamento rumore", "description": "Testo finale in approvazione da parte dell'ufficio legale", "priority": "medium"},
                    ],
                },
                {
                    "title": "Completati",
                    "tickets": [
                        {"title": "Aggiornamento policy privacy", "description": "Policy aggiornata in conformità al nuovo regolamento UE, pubblicata in wiki interna", "priority": "low"},
                        {"title": "Inventario archivio storico", "description": "Censimento completo: 12.000 documenti catalogati", "priority": "low"},
                    ],
                },
            ],
        },
    ],
    "canvases": [
        {
            "title": "Flusso Approvazione Delibere",
            "slug": "flusso-delibere",
            "description": "Percorso di approvazione delle delibere di giunta comunale",
            "data": _canvas_data(
                nodes=[
                    {"id": "n1", "type": "sticky", "position": {"x": 300, "y": 50}, "data": {"label": "Richiesta\nSettore proponente", "color": "#fbbf24"}},
                    {"id": "n2", "type": "sticky", "position": {"x": 150, "y": 200}, "data": {"label": "Redazione testo\nUfficio Legale", "color": "#60a5fa"}},
                    {"id": "n3", "type": "sticky", "position": {"x": 450, "y": 200}, "data": {"label": "Parere Dirigente\nDelibera", "color": "#60a5fa"}},
                    {"id": "n4", "type": "sticky", "position": {"x": 300, "y": 370}, "data": {"label": "Consiglio Comunale\nApprovazione", "color": "#a78bfa"}},
                    {"id": "n5", "type": "sticky", "position": {"x": 150, "y": 520}, "data": {"label": "Pubblicazione\nAlbo Pretorio", "color": "#34d399"}},
                    {"id": "n6", "type": "sticky", "position": {"x": 450, "y": 520}, "data": {"label": "Esecuzione\nSettore competente", "color": "#34d399"}},
                ],
                edges=[
                    {"id": "e1", "source": "n1", "target": "n2"},
                    {"id": "e2", "source": "n1", "target": "n3"},
                    {"id": "e3", "source": "n2", "target": "n4"},
                    {"id": "e4", "source": "n3", "target": "n4"},
                    {"id": "e5", "source": "n4", "target": "n5"},
                    {"id": "e6", "source": "n4", "target": "n6"},
                ],
            ),
        },
    ],
}


DEMO_PRESETS = [
    PRESET_AMMINISTRAZIONE,
    PRESET_STUDENTI,
    PRESET_AZIENDA,
    PRESET_EVENTO,
    PRESET_SCUOLA,
    PRESET_COMUNE_BOLZANO_PUBBLICO,
    PRESET_COMUNE_BOLZANO_INTERNO,
]

DEMO_PRESETS_BY_ID = {p["id"]: p for p in DEMO_PRESETS}

# SIPAT Project Contributor Guidelines (`AGENTS.md`)

This guide outlines the basic coding, version control, and testing standards for everyone (and any AI agent) writing code for `sipat-app`, `sipat-ml`, and `sipat`.

---

## 1. Code Writing & Structure

Keep code modular, clean, and separated into clear layers:

- **TypeScript (Frontend: `sipat-app` / `sipat`):** Always define types. Never use `any`.
- **Python (Backend: `sipat-ml`):** Use strict type hints and follow standard PEP 8 formatting.
- **Separation of Concerns:** Keep your UI screens separate from your database logic and mathematical processing scripts.

---

## 2. Object-Oriented Programming (OOP)

Organize processing engines (like the ML pipeline and telemetry systems) using basic OOP principles:

- **Classes:** Group related logic together (e.g., a `GPSProcessor` class or an `IPMTransformer` class).
- **Encapsulation:** Keep internal variables private. Modify data states only through clear public methods.
- **Interfaces:** Define clear input and output boundaries so different system parts can plug into each other easily.

---

## 3. Git Version Control Workflow

We use a simple, clean branching and commit strategy:

- **Branch Names:** Match your active issue ticket:
  - `feat/SIPAT-APP-[TicketID]-[short-name]`
  - `fix/SIPAT-APP-[TicketID]-[short-name]`
- **Atomic Commits:** Make small, frequent commits that fix or add exactly _one_ specific thing at a time.
- **Pull Requests:** Never push code directly to the `main` branch. Submit a Pull Request (PR) for review first.

---

## 4. CI/CD Pipeline

Every time code is pushed or a PR is opened, an automated system runs these checks:

1. **Linting:** Scans code for syntax and formatting errors.
2. **Testing:** Runs the test suites to ensure nothing is broken.
3. **Building:** Verifies that the app compiles successfully (Expo for mobile, Next.js for web, Docker for backend).

---

## 5. Automated Testing Rules

Write basic automated tests to protect critical system logic:

- **Unit Tests:** Verify mathematical logic, like the bounding box area percentages and severity thresholds.
- **Mock Testing:** Use fake datasets to simulate losing cellular network connection or experiencing indoor GPS drift.
- **Coverage:** Ensure any changes to structural code maintain basic test coverage so features stay reliable.

- use skills inside.agents/skills and also the global skills
- Detect when a skill applies.
- Invoke the skill tool.
- Follow the skill exactly.

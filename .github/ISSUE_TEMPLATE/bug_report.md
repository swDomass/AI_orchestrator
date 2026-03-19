---
name: Bug report
about: Report a bug in the AI Orchestrator
title: '[BUG] '
labels: bug
assignees: ''

---

**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior:
1.
2.
3.

**Expected behavior**
What did you expect to happen?

**Actual behavior**
What happened instead?

**Queue task (if applicable)**
Paste the relevant queue entry from `agent-queue.md`:
```markdown
- [ ] Your task here #tool:... #agent:...
```

**Logs**
Relevant output from `logs/orchestrator.log` (last ~50 lines around the error):
```
paste logs here
```

**Doctor output**
Output of `python orchestrator.py --doctor`:
```
paste here
```

**Environment**
- OS: [e.g. Windows 11, Ubuntu 22.04]
- Python version: [e.g. 3.11.4]
- Provider(s) affected: [e.g. Claude, Gemini, Codex, all]
- Orchestrator version / commit: [e.g. `git rev-parse --short HEAD`]

**Additional context**
Any other relevant context (profile used, parallel tasks, policy rules, etc.).

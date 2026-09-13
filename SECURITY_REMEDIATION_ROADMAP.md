# SECURITY REMEDIATION ROADMAP
## Prioritized Implementation Plan — Phase 2 Ready

**Status:** 🔴 AWAITING APPROVAL — Do not implement until approved  
**Total Effort Estimate:** 104 hours across 8 weeks  
**Target Completion:** October 24, 2026 (if started Sept 16)

---

## EXECUTIVE QUICK START

### Immediate Actions (Today)
1. ✅ **Review Phase 1 audit**: SECURITY_AUDIT_PHASE1.md
2. 📋 **Approve/modify roadmap**: Changes to priorities, timeline, scope
3. 🚀 **Approve Phase 2 start**: Begin implementation

### Approval Required

```markdown
[ ] Approve Phase 1 audit as-is
[ ] Modify high-priority findings (FINDING-001 to FINDING-005)
[ ] Approve Phase 1 timeline (2 weeks for critical path)
[ ] Approve Phase 2 timeline (4 weeks for enterprise hardening)
[ ] Approve Phase 3 (ongoing governance)
```

---

## PHASE 1: CRITICAL PATH (Weeks 1–2, 24 hours)

**Goal:** Production-safe baseline  
**Success Criteria:** All must-haves checked before production deployment

### Sprint 1.1: Secrets & Configuration (Week 1, Day 1–2)

#### Task 1.1.1: Pre-flight Secret Validation
**Finding:** FINDING-001  
**Effort:** 3 hours  
**Priority:** 🔴 Critical  
**Owner:** Backend Lead / DevOps  

**Deliverables:**
1. Add FastAPI lifespan context manager (`@app.lifespan()`)
2. Pre-flight checks:
   - Verify .env file exists
   - Verify all mandatory secrets are set (OLLAMA_HOST, DB_CONNECTION, etc.)
   - Verify optional secrets are consistent (if ENABLE_WEB_SEARCH=true, require WEB_SEARCH_API_KEY)
3. Connectivity test: Ping Ollama, DB, embeddings provider
4. Fail fast with actionable error messages
5. Add to docs/DEPLOYMENT.md: "Pre-Flight Checklist"

**Definition of Done:**
- [ ] FastAPI lifespan adds startup checks in `on_startup` event
- [ ] Test with missing .env, missing keys, unreachable services
- [ ] Error messages guide user to fix (e.g., "Set OLLAMA_HOST=http://localhost:11434")
- [ ] CI test: startup fails correctly with missing secrets
- [ ] Documentation updated

**Code Locations:**
- api/main.py (add lifespan context manager)
- tests/test_startup_validation.py (new)
- docs/DEPLOYMENT.md (add checklist)

---

#### Task 1.1.2: Environment Variable Validation
**Finding:** FINDING-001  
**Effort:** 2 hours  
**Priority:** 🔴 Critical  
**Owner:** Backend Lead

**Deliverables:**
1. Add to config/settings.py:
   - Validate conflicting settings (HIGH_COST_THRESHOLD must be >= MODERATE_COST_THRESHOLD)
   - Validate file paths exist (if DATABASE_URL uses SQLite file path)
   - Validate numeric ranges (rate limits must be positive)
2. Test coverage: all validation failures
3. Update .env.example with guidance comments

---

### Sprint 1.2: Security Testing in CI/CD (Week 1, Day 2–3)

#### Task 1.2.1: Add Automated Security Scanning
**Finding:** FINDING-002  
**Effort:** 4 hours  
**Priority:** 🔴 Critical  
**Owner:** DevOps / Security Engineer  

**Deliverables:**
1. Create `.github/workflows/security.yml`
2. Integrate tools:
   - **pip-audit**: Check for known CVEs in dependencies
   - **bandit**: Python security linter (SAST)
   - **Security test suite**: Run `pytest -m security`
3. Workflow fails if:
   - Known CVEs found (fail)
   - Bandit finds High/Medium severity issues (review required)
   - Security tests fail
4. Post results as PR comment

**CI/CD Configuration:**
```yaml
# .github/workflows/security.yml
name: Security Scan

on: [pull_request, push]

jobs:
  security:
	runs-on: ubuntu-latest
	steps:
	  - uses: actions/checkout@v4
	  - uses: actions/setup-python@v5
		with:
		  python-version: '3.14'

	  # Dependency vulnerability scan
	  - run: pip install pip-audit && pip-audit

	  # Python security linter
	  - run: pip install bandit && bandit -r . -ll

	  # Security test suite
	  - run: pip install -e . && pytest -m security -v
```

---

#### Task 1.2.2: Add Security Test Suite
**Finding:** FINDING-002  
**Effort:** 5 hours  
**Priority:** 🔴 Critical  
**Owner:** Security Engineer  

**Deliverables:**
1. Create `tests/test_security_all.py` (add @pytest.mark.security to all existing security tests)
2. Add new security tests:
   - `test_prompt_injection_basic()`: Common injection patterns
   - `test_prompt_injection_unicode()`: Homoglyph & homophone attacks
   - `test_sql_injection_various()`: Test SQL validator against known payloads
   - `test_rate_limit_enforcement()`: Per-IP, per-action limits
   - `test_auth_required()`: Endpoints reject missing Authorization header
   - `test_secret_not_logged()`: Verify API keys don't appear in logs
   - `test_error_no_schema_leak()`: Verify errors don't expose table/column names
3. Create `conftest.py` fixture for security test markers
4. Document security test coverage in README

**Test Coverage Target:**
- Security paths: 90%+ coverage
- Critical functions (SQL validator, input guard, auth): 95%+

---

### Sprint 1.3: Data Retention & Policy (Week 1, Day 3–4)

#### Task 1.3.1: Define Data Retention Policy
**Finding:** FINDING-004  
**Effort:** 2 hours  
**Priority:** 🔴 Critical  
**Owner:** Security + Legal (if applicable)

**Deliverables:**
1. Add `docs/DATA_RETENTION_POLICY.md`:
   ```markdown
   # Data Retention Policy

   ## Audit Logs
   - Retention: 90 days
   - Rotation: Daily (compressed)
   - Deletion: Automatic after 90 days

   ## Query Results (in-memory cache)
   - Retention: 1 session (max 1 hour)
   - Eviction: LRU on memory pressure

   ## Embeddings (ChromaDB)
   - Retention: Until document deleted
   - Deletion: When document status = 'deleted'

   ## Backup Data
   - Retention: 30 days
   - Location: Encrypted separate storage
   - DPO oversight: Yes

   ## PII/Sensitive Queries
   - Retention: 7 days (audit logs only, results not stored)
   - Deletion on request: 24 hours
   ```
2. Update SECURITY.md with link to policy
3. Create deletion procedures document

**Definition of Done:**
- [ ] Policy documents in docs/
- [ ] SECURITY.md references policy
- [ ] Legal/compliance review complete

---

#### Task 1.3.2: Implement Log Rotation
**Finding:** FINDING-004  
**Effort:** 3 hours  
**Priority:** 🔴 Critical  
**Owner:** Backend / DevOps

**Deliverables:**
1. Configure Python logging rotation:
   - Add `RotatingFileHandler` to security/audit_log.py
   - Max file size: 10 MB
   - Backup count: 9 (90 MB total, per 90-day retention)
   - Compression: gzip old logs
2. Add scheduled cleanup job:
   - Delete logs older than 90 days
   - Run daily at 2 AM (off-peak)
3. Test log rotation and cleanup
4. Document in ops guide

**Code Changes:**
- security/audit_log.py (add RotatingFileHandler)
- scripts/cleanup_old_logs.py (new, run via cron/scheduler)

---

### Sprint 1.4: Moderation Hardening (Week 2, Day 1–2)

#### Task 1.4.1: Add User Input Moderation
**Finding:** FINDING-005  
**Effort:** 3 hours  
**Priority:** 🔴 Critical  
**Owner:** Backend

**Deliverables:**
1. Add optional moderation check before SQL validator:
   - Gate applied: api/main.py question submission endpoint
   - Check: `moderation.check_content(question, category='user_input')`
   - Fallback: If moderation disabled, allow all (current behavior)
2. Moderation response:
   - Pass: Continue to SQL validator
   - Fail: Return 400 with reason (do not pass to agent)
   - Error: Fail open (warn, but allow)
3. Test coverage: moderation failures, moderation disabled, false positives

**Code Locations:**
- api/main.py (add moderation check in /ask endpoint)
- moderation/check.py (extend check_content for user_input category)
- tests/test_moderation_user_input.py (new)

---

#### Task 1.4.2: Health Check for Moderation Provider
**Finding:** FINDING-005  
**Effort:** 2 hours  
**Priority:** 🔴 Critical  
**Owner:** Backend

**Deliverables:**
1. Add health check endpoint for moderation provider:
   - POST /health calls moderation provider
   - Returns: `{"status": "ok", "provider": "openai"}` or `{"status": "error", "reason": "..."}` 
2. Frontend displays moderation status
3. Fail-open configurable: MODERATION_FAIL_OPEN=true (default, allows requests if provider down)
4. Test: moderation provider down, API still works (with warning)

---

### Sprint 1.5: Documentation & Testing (Week 2, Day 2–3)

#### Task 1.5.1: Update Security Documentation
**Finding:** Multiple  
**Effort:** 2 hours  
**Owner:** Security

**Deliverables:**
1. Add to SECURITY.md:
   - Data retention policy (link to docs/DATA_RETENTION_POLICY.md)
   - Pre-flight validation steps
   - Moderation architecture & fallback behavior
   - Rate limiting tiers & limits per component
   - Error sanitization approach
2. Create DEPLOYMENT.md updates:
   - Secrets checklist
   - Database user configuration (verify read-only)
   - Reverse proxy requirements (HTTPS, rate limiting, auth)
   - Monitoring & alerting setup

---

#### Task 1.5.2: Phase 1 Acceptance Test
**Finding:** All  
**Effort:** 2 hours  
**Owner:** QA / Security

**Deliverables:**
1. Test checklist:
   - [ ] Startup fails with missing .env
   - [ ] Startup checks connectivity to Ollama, DB
   - [ ] Security tests pass in CI/CD
   - [ ] API key not exposed in logs
   - [ ] Rate limit 429 response works
   - [ ] Query timeout enforced
   - [ ] Moderation health check available
   - [ ] Log rotation working
   - [ ] Old logs deleted after 90 days
2. Document test results in SECURITY_AUDIT_PHASE1.md

---

## PHASE 2: ENTERPRISE HARDENING (Weeks 3–6, 48 hours)

**Goal:** Enterprise compliance, multi-instance deployment, SIEM-ready  
**Success Criteria:** Production deployment with monitoring & governance

### Sprint 2.1: SIEM Integration & Monitoring (Week 3–4, 10 hours)

#### Task 2.1.1: Structured Logging Export
**Finding:** FINDING-006  
**Effort:** 4 hours  
**Priority:** 🟡 High  
**Owner:** Backend / DevOps  

**Deliverables:**
1. Export audit logs in JSON-Lines format:
   - Each line = one JSON event
   - Fields: timestamp, correlation_id, event_type, user, action, status, resource, details
2. Add optional syslog export (RFC3164 or RFC5424)
3. Add log shipping to:
   - Splunk HTTP Event Collector (HEC)
   - ELK Stack (Elasticsearch)
   - Datadog Agent
4. Configuration:
   - ENABLE_SIEM_EXPORT=true
   - SIEM_ENDPOINT=https://splunk.example.com/services/collector
   - SIEM_TOKEN=hec-token

**Code Changes:**
- security/audit_log.py (export to JSON-Lines)
- config/settings.py (add SIEM_* settings)
- scripts/siem_templates/ (new, Splunk/ELK configs)

---

#### Task 2.1.2: Basic Real-Time Alerting
**Finding:** FINDING-006  
**Effort:** 3 hours  
**Priority:** 🟡 High  
**Owner:** Backend  

**Deliverables:**
1. Add alerting on:
   - 5 consecutive rate limit hits (from same IP)
   - 3 consecutive auth failures (same IP)
   - Cost limit exceeded (high_cost status)
   - Moderation provider down (health check fails)
2. Alert delivery:
   - Log at WARNING level (picked up by SIEM)
   - Optional webhook (POST to configured URL)
   - Configurable threshold & cooldown

**Code:**
- agent/alerting.py (new)
- config/settings.py (ALERT_WEBHOOK_URL, thresholds)

---

#### Task 2.1.3: Security Dashboard / Health Endpoint
**Finding:** FINDING-006  
**Effort:** 3 hours  
**Owner:** Backend  

**Deliverables:**
1. Enhance `/health` endpoint:
   ```json
   {
	 "status": "ok",
	 "components": {
	   "database": "ok",
	   "ollama": "ok",
	   "moderation": "ok"
	 },
	 "security": {
	   "rate_limits": {
		 "questions_per_minute": {"limit": 10, "current": 3},
		 "llm_calls_per_minute": {"limit": 20, "current": 18}
	   },
	   "recent_alerts": [
		 {"event": "rate_limit_hit", "count": 5, "ip": "10.0.1.5", "time": "2026-09-15T14:22:00Z"}
	   ]
	 }
   }
   ```
2. Frontend dashboard:
   - Display real-time rate limit usage
   - Display recent security events
   - Display moderation provider status
3. Monitoring tool integration (export Prometheus metrics)

---

### Sprint 2.2: Dependency & Vulnerability Management (Week 3, 5 hours)

#### Task 2.2.1: Dependency Scanning & Update Policy
**Finding:** FINDING-007  
**Effort:** 3 hours  
**Owner:** DevOps  

**Deliverables:**
1. Enable Dependabot:
   - GitHub settings → Code security → Dependabot enabled
   - Configure: `dependabot.yml` with weekly checks
2. Add pyproject.toml:
   - Pin Python version: `python = ">=3.11,<3.15"`
   - Pin base dependencies per requirements.txt
3. Create dependency update policy:
   - Patch updates (0.0.X): Auto-merge if tests pass
   - Minor updates (0.X.0): PR, manual review
   - Major updates (X.0.0): PR, security review, vendor vetting
4. Prepare pip-audit requirements file

**Configuration:**
```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: "pip"
	directory: "/"
	schedule:
	  interval: "weekly"
	open-pull-requests-limit: 5
	reviewers:
	  - "security-team"
```

---

#### Task 2.2.2: Supply Chain Risk Assessment
**Finding:** FINDING-007  
**Effort:** 2 hours  
**Owner:** Security  

**Deliverables:**
1. Audit critical dependencies for:
   - Maintenance status (is it actively maintained?)
   - Security track record (CVE history)
   - Author reputation (verified publisher?)
2. Create risk register for top 10 dependencies:
   - langgraph (LangChain team — trusted)
   - ollama (Ollama project — trusted)
   - sqlglot (Tobig.tech — trusted)
   - chromadb (startup — medium risk, but widely used)
3. Recommend alternatives if risk too high
4. Document in docs/DEPENDENCY_RISK_REGISTER.md

---

### Sprint 2.3: Error Message & Output Sanitization (Week 4, 6 hours)

#### Task 2.3.1: Comprehensive Error Redaction Audit
**Finding:** FINDING-008  
**Effort:** 3 hours  
**Owner:** Security Engineer + Backend  

**Deliverables:**
1. Systematically test error messages from all databases:
   - PostgreSQL: Connection failures, syntax errors, permission errors
   - MySQL: Same error categories
   - MSSQL: Same error categories
   - Oracle: Same error categories
2. Test queries designed to leak information:
   - `SELECT * FROM nonexistent_table` (should not leak table names in schema)
   - `SELECT nonexistent_column FROM t` (should not leak column names)
   - Connection errors with passwords
3. Verify `security/redaction.py` catches all problematic patterns
4. Add to CI: Integration test against real databases (Docker containers)

**Code:**
- tests/test_error_redaction_e2e.py (new, database-specific)
- security/redaction.py (audit & extend regex patterns)

---

#### Task 2.3.2: Logging Sanitization Policy
**Finding:** FINDING-008  
**Effort:** 2 hours  
**Owner:** Backend  

**Deliverables:**
1. Add logging best practices guide in SECURITY.md:
   - Never log: passwords, API keys, full SQL queries (only log hash), full results
   - Always log: user identity, action taken, timestamp, status
   - Provide redacted example
2. Add automated log redaction:
   - Regex patterns for secrets (AWS keys, API tokens)
   - Automatic application to all log messages
3. Test: Verify secrets don't leak even with `%r` formatting

**Code:**
- security/logging_redaction.py (new, wraps logger)
- config/logging.py (apply redaction to all handlers)

---

#### Task 2.3.3: Frontend Error Display
**Finding:** FINDING-008  
**Effort:** 1 hour  
**Owner:** Frontend  

**Deliverables:**
1. Update React error boundaries:
   - Show generic message to user ("Query failed, please try again")
   - Log full error to browser console (dev only)
   - Never render error.message in UI
2. Test: Verify no schema info leaks in error messages

---

### Sprint 2.4: Rate Limiting & Distributed Deployment (Week 5, 8 hours)

#### Task 2.4.1: Redis-Backed Rate Limiter
**Finding:** FINDING-009  
**Effort:** 5 hours  
**Priority:** 🟡 High (multi-instance deployments)  
**Owner:** Backend / DevOps  

**Deliverables:**
1. Add Redis as optional dependency:
   - `REDIS_URL` in config
   - Fallback: In-memory limiter if Redis unavailable
2. Refactor rate limiters:
   - `agent/rate_limit.py`: Add RedisRateLimiter class
   - `api/rate_limit.py`: Use Redis for per-IP tracking
3. Per-user rate limiting (requires FINDING-003):
   - Track per (user_id + resource_type)
   - Stricter limits for expensive operations
4. Configuration:
   ```
   RATE_LIMIT_BACKEND=redis  # or "memory"
   REDIS_URL=redis://localhost:6379/0
   ```

**Code:**
- agent/rate_limit.py (add RedisRateLimiter)
- api/rate_limit.py (update to use Redis)
- config/settings.py (add REDIS_URL, RATE_LIMIT_BACKEND)
- docker-compose.yml (add Redis service)
- tests/test_redis_rate_limit.py (new)

---

#### Task 2.4.2: Distributed Deployment Testing
**Finding:** FINDING-009  
**Effort:** 3 hours  
**Owner:** DevOps / QA  

**Deliverables:**
1. Test multi-instance deployment:
   - Spin up 3 API instances + 1 Redis
   - Verify rate limits enforced globally (not per-instance)
2. Test rate limit bypass scenarios:
   - Rapid requests from 10 different IPs (should fail overall)
   - Requests after rate limit reset (should succeed)
3. Document load balancing setup (nginx round-robin example)

---

### Sprint 2.5: Agent State Integrity (Week 5, 6 hours)

#### Task 2.5.1: State Versioning & Snapshots
**Finding:** FINDING-010  
**Effort:** 4 hours  
**Owner:** Backend  

**Deliverables:**
1. Add state versioning to agent/state.py:
   - Every node stores: `state_version = state.get("_version", 0) + 1`
   - Checkpoint: Save state at each node transition
   - Enable rollback if needed
2. State integrity check:
   - Compute hash of initial state
   - Verify hash unchanged between nodes
   - Log any tampering attempts
3. Immutable state fields:
   - Mark `question`, `user_id`, `session_id` as read-only
   - Raise exception if mutated

**Code:**
- agent/state.py (add _version, _checksum fields)
- agent/graph.py (add state snapshots at node boundaries)
- tests/test_state_integrity.py (new)

---

### Sprint 2.6: Container Hardening (Week 5–6, 4 hours)

#### Task 2.6.1: Run as Non-Root User
**Finding:** FINDING-012  
**Effort:** 2 hours  
**Owner:** DevOps  

**Deliverables:**
1. Update Dockerfile:
   ```dockerfile
   # Add non-root user
   RUN useradd -m -u 1000 appuser
   USER appuser

   # Ensure permissions for app directory
   RUN mkdir -p /app && chown appuser:appuser /app
   ```
2. Test: Verify app still runs, logs are writable
3. Add to docker-compose.yml: user: "1000:1000"

---

#### Task 2.6.2: Resource Limits & Security Options
**Finding:** FINDING-012  
**Effort:** 2 hours  
**Owner:** DevOps  

**Deliverables:**
1. Add to docker-compose.yml:
   ```yaml
   services:
	 api:
	   deploy:
		 resources:
		   limits:
			 cpus: '2'
			 memory: 2G
		   reservations:
			 cpus: '1'
			 memory: 1G
	   security_opt:
		 - no-new-privileges:true
	   read_only: true
	   tmpfs:
		 - /tmp
   ```
2. Health checks in docker-compose
3. Test: Verify app runs with resource limits

---

### Sprint 2.7: Encryption Configuration (Week 6, 6 hours)

#### Task 2.7.1: Encryption Documentation
**Finding:** FINDING-013  
**Effort:** 3 hours  
**Owner:** Security / DevOps  

**Deliverables:**
1. Add docs/ENCRYPTION_GUIDE.md:
   - TLS/HTTPS configuration (reverse proxy responsibility)
   - Encryption at-rest options (ChromaDB, audit logs)
   - Key management for production
   - Data classification guide
2. Deployment template:
   - Nginx with TLS config (example)
   - PostgreSQL SSL configuration (example)
   - Backup encryption (example with encfs or dm-crypt)

---

#### Task 2.7.2: Secret Storage Recommendation
**Finding:** FINDING-001 & FINDING-013  
**Effort:** 2 hours  
**Owner:** Security / DevOps  

**Deliverables:**
1. Add to docs/DEPLOYMENT.md:
   - For Docker: Secrets management options (Docker Secrets, HashiCorp Vault, AWS Secrets Manager)
   - For Kubernetes: Sealed Secrets, External Secrets Operator
   - For cloud: Cloud-native key vaults (AWS KMS, Azure Key Vault, GCP Secret Manager)
2. Example implementations for one platform (e.g., AWS)

---

### Sprint 2.8: PII Detection & Masking (Week 6, 8 hours) — OPTIONAL

#### Task 2.8.1: PII Detection Integration
**Finding:** FINDING-011  
**Effort:** 5 hours  
**Priority:** 🟡 Optional (depends on data sensitivity)  
**Owner:** Backend  

**Deliverables:**
1. Add optional PII detection:
   - Integrate `presidio-analyzer` for named entity recognition
   - Detect: email, phone, SSN, credit card, passport, etc.
2. Classification:
   - Automatically tag sensitive columns when sampled
   - Manual tagging via config/sensitive_columns.yaml
3. Result masking:
   - Option to mask PII in results (e.g., "***-**-1234" for SSN)
   - Log indicates values were masked, not removed
4. Configuration:
   ```
   ENABLE_PII_DETECTION=true
   PII_MASK_RESULTS=true
   PII_MASK_FORMAT=partial  # or "redact"
   ```

**Code:**
- agent/pii_detector.py (new, wrapper around presidio)
- agent/result_formatting.py (add masking logic)
- config/settings.py (add PII settings)

---

## PHASE 3: GOVERNANCE & RED TEAMING (Weeks 7–12, ongoing)

**Goal:** Formal security governance, red teaming, compliance maturity  
**Success Criteria:** Annual red team exercise, zero critical findings

### Sprint 3.1: Formal Threat Modeling (Week 7–8, 8 hours)

#### Task 3.1.1: STRIDE Threat Model
**Finding:** FINDING-014  
**Effort:** 5 hours  
**Owner:** Security Architect + Team  

**Deliverables:**
1. Apply STRIDE to each data source:
   - **SQL**: Spoofing (token reuse), Tampering (malicious SQL), Repudiation (who ran query?), Information Disclosure (schema leak), Denial of Service (expensive queries), Elevation of Privilege (write attempt)
   - **Documents**: Spoofing (who uploaded?), Tampering (malicious PDFs), Repudiation, Info Disclosure (PII), DoS (huge files), EoP
   - **Policy**: Same as documents
   - **Web Search**: Spoofing (search poisoning), Tampering (MITM), Repudiation, Info Disclosure, DoS, EoP
   - **Generation**: Tampering (jailbreak attempts), Info Disclosure (model secrets), DoS (token abuse), EoP
   - **Media Search**: Similar to web search + media handling risks
2. Document in docs/THREAT_MODEL_STRIDE.md
3. Create risk register: likelihood × impact for each threat

---

#### Task 3.1.2: Attack Tree Analysis
**Finding:** FINDING-014  
**Effort:** 3 hours  
**Owner:** Security Architect  

**Deliverables:**
1. Create attack trees for high-value targets:
   - "Attacker exfiltrates PII via SQL injection" → Prevention controls
   - "Attacker jailbreaks model to access system prompt" → Prevention controls
   - "Attacker costs DoS via expensive queries" → Prevention controls
2. Document in docs/ATTACK_TREES.md
3. Map each attack path to control(s) that mitigate it

---

### Sprint 3.2: Red Team Exercise (Week 9–10, 12 hours)

#### Task 3.2.1: Schedule & Scope Red Team
**Finding:** FINDING-015  
**Effort:** 2 hours  
**Owner:** Security Lead  

**Deliverables:**
1. Red team exercise plan:
   - Date: December 2026 (Q4)
   - Duration: 1 week full-time (40 hours)
   - Scope: Full-stack (API, UI, LLM integration, RAG)
   - Rules of engagement: Agreed beforehand
   - Reporting: Detailed findings + remediation recommendations
2. Invite vendor or internal team with security expertise
3. Document outcomes in SECURITY_AUDIT_PHASE2_RED_TEAM.md

---

#### Task 3.2.2: Address Red Team Findings
**Finding:** FINDING-015  
**Effort:** 10 hours  
**Owner:** Full team  

**Deliverables:**
1. Triage red team findings (likely P0/P1/P2)
2. Create follow-up issues for each
3. Remediate P0 findings immediately
4. Schedule P1/P2 in roadmap

---

### Sprint 3.3: Session & Timeout Management (Week 7, 4 hours)

#### Task 3.3.1: Session Timeout
**Finding:** FINDING-016  
**Effort:** 2 hours  
**Owner:** Backend  

**Deliverables:**
1. Add session timeout (requires FINDING-003 multi-user auth):
   - Session max age: 1 hour
   - Inactivity timeout: 30 minutes
   - Refresh token valid for 7 days
2. Cleanup: Delete session state on timeout
3. Test: Verify expired sessions rejected

---

#### Task 3.3.2: Session Cleanup Job
**Finding:** FINDING-016  
**Effort:** 2 hours  
**Owner:** Backend  

**Deliverables:**
1. Add scheduled cleanup:
   - Run every 30 minutes
   - Delete expired sessions
   - Delete orphaned state
2. Monitor cleanup job success
3. Test: Verify cleanup doesn't interfere with active sessions

---

### Sprint 3.4: Authentication Audit Logging (Week 8, 3 hours)

#### Task 3.4.1: Log Successful & Failed Authentication
**Finding:** FINDING-017  
**Effort:** 3 hours  
**Owner:** Backend  

**Deliverable:**
1. Add to security/audit_log.py:
   - `log_auth_attempt(username, status, ip, timestamp, device_fingerprint)`
   - Status: "success", "failed_invalid_token", "failed_expired", "blocked_rate_limit"
2. Include in /health endpoint's recent_events
3. Alert on 5+ failed attempts in 5 minutes

---

### Sprint 3.5: Disaster Recovery & Business Continuity (Week 8–9, 4 hours)

#### Task 3.5.1: Backup & Recovery Procedures
**Finding:** FINDING-018  
**Effort:** 3 hours  
**Owner:** DevOps / SRE  

**Deliverables:**
1. Document RTO/RPO targets:
   - RTO (Recovery Time Objective): 1 hour
   - RPO (Recovery Point Objective): 15 minutes
2. Create backup procedures:
   - Database: Automated daily snapshots
   - ChromaDB: Nightly tar + S3 upload
   - Audit logs: Daily rotation + 90-day retention
3. Recovery procedures:
   - Test database from backup
   - Test ChromaDB recovery from tarball
   - Documented steps (runbook format)
4. Schedule recovery drill: Quarterly

**Documentation:**
- docs/DISASTER_RECOVERY.md

---

### Sprint 3.6: Security Assumptions Documentation (Week 9, 2 hours)

#### Task 3.6.1: Explicit Assumptions & Responsibility Matrix
**Finding:** FINDING-019  
**Effort:** 2 hours  
**Owner:** Security Architecture  

**Deliverables:**
1. Add to SECURITY.md:
   ```markdown
   ## Application Responsibility (What This App Guarantees)
   - SQL validation & read-only enforcement
   - Input sanitization & injection prevention
   - Secret redaction in logs
   - Rate limiting per-IP, per-action
   - Bearer token validation
   - Audit logging of all attempts

   ## Deployment Responsibility (What Your Infra Must Provide)
   - TLS/HTTPS encryption in transit
   - Network isolation (firewall rules)
   - Reverse proxy authentication (optional, but recommended)
   - Database user privilege enforcement
   - Log aggregation & retention
   - Secrets management (external vault)
   - Monitoring & alerting (SIEM)
   ```
2. Create responsibility matrix (app vs. infra team)

---

### Sprint 3.7: Code Ownership & Change Control (Week 10, 3 hours)

#### Task 3.7.1: CODEOWNERS File
**Finding:** FINDING-020  
**Effort:** 1 hour  
**Owner:** Engineering Lead  

**Deliverables:**
1. Create .github/CODEOWNERS:
   ```
   # Security-critical paths require review from security team
   /agent/sql_validator.py @security-team
   /security/ @security-team @backend-lead
   /api/auth.py @security-team
   /rag/ @security-team @backend-lead
   /config/sensitive_columns.yaml @security-team

   # Change management
   /Dockerfile @devops-team @security-team
   /.github/workflows/security.yml @devops-team @security-team
   ```
2. Enforce in GitHub branch protection

---

#### Task 3.7.2: Change Management Process
**Finding:** FINDING-020  
**Effort:** 2 hours  
**Owner:** Security Lead  

**Deliverables:**
1. Document security change process:
   - All changes to security-critical files require 2 approvals
   - At least 1 approval from security-team
   - Automated tests must pass
   - No bypassing of branch protection
2. Create security change template (PR):
   ```markdown
   ## Security Change Checklist
   - [ ] Threat modeling: What new risks does this introduce?
   - [ ] Tests: Security tests pass (not just functional)
   - [ ] Documentation: Security.md / docs/ updated
   - [ ] Deployment: Any infra changes needed?
   - [ ] Monitoring: Any new alerts needed?
   ```

---

### Sprint 3.8: Test Coverage Benchmarking (Week 10–11, 4 hours)

#### Task 3.8.1: Coverage Analysis
**Finding:** FINDING-021  
**Effort:** 2 hours  
**Owner:** QA / Backend  

**Deliverables:**
1. Run coverage analysis:
   ```bash
   pytest --cov=. --cov-report=html
   ```
2. Focus on critical modules:
   - agent/sql_validator.py: Target 95%+
   - agent/input_guard.py: Target 95%+
   - security/: Target 90%+
   - rag/: Target 85%+
3. Create coverage report in CI/CD
4. Enforce minimum coverage in PR (no downward drift)

---

#### Task 3.8.2: Security Test Expansion
**Finding:** FINDING-021  
**Effort:** 2 hours  
**Owner:** QA / Security  

**Deliverables:**
1. Add adversarial test suite:
   - Fuzzing (random inputs)
   - Boundary testing (edge cases)
   - Combinatorial testing (X + Y attack vectors)
2. Create /tests/test_security_fuzzing.py
3. Run fuzzing in CI/CD (limited iterations)

---

## IMPLEMENTATION SUMMARY

### Timeline & Capacity

| Phase | Duration | Effort | Team Size | Velocity |
|-------|----------|--------|-----------|----------|
| **Phase 1** | 2 weeks | 24 hours | 3–4 people | 12 hrs/week |
| **Phase 2** | 4 weeks | 48 hours | 4–5 people | 12 hrs/week |
| **Phase 3** | 6 weeks+ | 32 hours | 2–3 people (ongoing) | 5–8 hrs/week |
| **TOTAL** | 8+ weeks | 104 hours | — | — |

### Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Scope creep | Fixed lanes per sprint; changes go to Phase 3 |
| Dependency on Ollama availability | Use mock Ollama in testing; fallback to local inference |
| Rate limiter Redis adding complexity | Start with in-memory, migrate to Redis in Phase 2.4 |
| PII detection slow/expensive | Make optional; test performance before enabling |
| Red team finding critical issues | Budget time in Q4 2026 for remediation |

---

## SUCCESS CRITERIA

### Phase 1 Complete
- [ ] All High-priority findings addressed
- [ ] CI/CD security tests passing
- [ ] Production deployment checklist signed off
- [ ] Moderation health check working
- [ ] Audit logs rotate after 90 days

### Phase 2 Complete
- [ ] SIEM integration documented & tested
- [ ] Multi-instance deployment tested
- [ ] Container runs as non-root
- [ ] Error messages audited for leaks
- [ ] PII detection optional feature working
- [ ] Distributed rate limiting with Redis

### Phase 3 Complete (Ongoing)
- [ ] Formal threat model (STRIDE) documented
- [ ] Annual red team exercise completed
- [ ] Security CODEOWNERS active
- [ ] Test coverage 90%+ for critical paths
- [ ] Session timeout enforced
- [ ] Backup/recovery procedures tested quarterly

---

## BUDGET & RESOURCES

### Team Composition (Recommended)

**Phase 1 (2 weeks, 24 hours):**
- 1 Security Engineer (40%)
- 1 Backend Engineer (40%)
- 1 DevOps Engineer (30%)

**Phase 2 (4 weeks, 48 hours):**
- 1 Security Engineer (40%)
- 1–2 Backend Engineers (60%)
- 1 DevOps Engineer (60%)

**Phase 3 (Ongoing, 32+ hours/quarter):**
- 1 Security Lead (20%)
- Full team on-call for issues

### Training Needs
- Security: OWASP Top 10 for LLM, STRIDE threat modeling
- Backend: Distributed rate limiting, PII detection
- DevOps: SIEM integration, container hardening
- All: Annual red team debrief

---

## APPROVAL GATES

### Gate 1: Before Phase 1 Starts
- [ ] Approve Phase 1 roadmap (findings prioritization)
- [ ] Assign team members
- [ ] Set Phase 1 deadlines (default: 2 weeks)

### Gate 2: Before Phase 2 Starts
- [ ] Phase 1 acceptance test passed
- [ ] No critical issues blocking Phase 2
- [ ] Approval for enterprise hardening scope

### Gate 3: Before Red Team
- [ ] Phase 2 acceptance test passed
- [ ] Threat model (STRIDE) completed
- [ ] Budget for red team engagement approved

### Gate 4: Final (Production Ready)
- [ ] All Phases 1–3 findings remediated
- [ ] Red team findings triaged & scheduled
- [ ] Security score 85+/100 (or approved exceptions)
- [ ] Compliance auditor sign-off (if required)

---

## NEXT STEPS

1. **Review** this roadmap with security leadership
2. **Approve** priority ranking, timeline, scope
3. **Assign** team members to sprints
4. **Create** GitHub issues from each task
5. **Begin** Phase 1 implementation

**Status:** 🔴 **AWAITING YOUR APPROVAL** to proceed

---

**Roadmap Version:** 1.0  
**Date:** September 13, 2026  
**Next Review:** After Phase 1 completion (Week 2)

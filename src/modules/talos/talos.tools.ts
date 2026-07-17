import { ToolDecorator as Tool, ExecutionContext, z } from '@nitrostack/core';
import { callTalosTool } from './talos.bridge.js';

const ScanWebsiteSchema = z.object({
  url: z.string().min(1).describe('Website or URL to scan, for example example.com or https://example.com.'),
});

const LookupCvesSchema = z.object({
  product: z.string().min(1).describe('Software or product name, for example nginx.'),
  version: z.string().optional().describe('Optional version string, for example 1.18.0.'),
});

const AnalyzeAuthLogSchema = z.object({
  path: z.string().optional().describe('Optional auth log path. Defaults to the Talos configured auth log.'),
  threshold: z.number().int().positive().optional().describe('Failed-attempt threshold for suspicious IPs.'),
});

const SearchResearchSchema = z.object({
  query: z.string().min(1).describe('Research query or topic.'),
  source: z.enum(['all', 'openalex', 'semantic_scholar', 'core']).optional().describe('Database to search. Defaults to all.'),
  year_from: z.number().int().optional().describe('Only include papers from this year onward.'),
  open_access: z.boolean().optional().describe('Only include papers with open access full text.'),
});

const FindResearchPapersSchema = z.object({
  query: z.string().optional().describe('Direct academic/research paper search query.'),
  topic: z.string().optional().describe('Topic to search for papers, citations, studies, or academic literature.'),
  findings: z.array(z.string()).optional().describe('Website scan findings or vulnerability names, such as Missing HSTS, Missing CSP, clickjacking, MIME sniffing, or Referrer-Policy.'),
  target: z.string().optional().describe('Optional website/domain context from the scan.'),
  product: z.string().optional().describe('Optional product/software context for vulnerability papers.'),
  version: z.string().optional().describe('Optional product version.'),
  source: z.enum(['all', 'openalex', 'semantic_scholar', 'core']).optional().describe('Database to search. Defaults to all.'),
  year_from: z.number().int().optional().describe('Only include papers from this year onward. Defaults to 2010.'),
  open_access: z.boolean().optional().describe('Only include papers with open access full text.'),
  max_results: z.number().int().min(1).max(16).optional().describe('Maximum papers to return. Defaults to 12.'),
});

const AnalyzeLinkSafetySchema = z.object({
  url: z.string().min(1).describe('URL or link to check for phishing, malware, scams, unsafe redirects, impersonation, and other link risks.'),
  context: z.string().optional().describe('Optional context, such as where the link came from: email, SMS, Discord, WhatsApp, ad, QR code, or website.'),
  expected_brand: z.string().optional().describe('Optional brand or service the link claims to be from, such as Google, Microsoft, PayPal, Instagram, or a bank.'),
  follow_redirects: z.boolean().optional().describe('Follow redirects and analyze the final destination. Defaults to true.'),
  check_providers: z.boolean().optional().describe('Use configured threat-intel providers such as Google Safe Browsing, VirusTotal, URLhaus, PhishTank, and urlscan. Defaults to true.'),
  privacy_mode: z.boolean().optional().describe('If true, do not query external threat-intel providers. Use this for private invite/reset/token links. Defaults to false in this test build.'),
  submit_to_sandbox: z.boolean().optional().describe('If true, allow submitting the URL to sandbox scanners such as urlscan. Defaults to false to avoid exposing private links.'),
});

const GenerateBlocklistSchema = z.object({
  ips: z.array(z.string()).min(1).describe('Attacking IP addresses to block.'),
  threshold: z.number().int().positive().optional().describe('fail2ban maxretry value. Defaults to 5.'),
});

const SendAlertSchema = z.object({
  message: z.string().min(1).describe('Security alert body.'),
  subject: z.string().optional().describe('Optional alert subject.'),
  to: z.string().email().optional().describe('Optional recipient email. Defaults to SECURITY_ALERT_EMAIL or ALERT_EMAIL.'),
});

const SendEmailSchema = z.object({
  message: z.string().min(1).describe('Email body. Include the important points or summary to forward.'),
  subject: z.string().optional().describe('Optional email subject. Defaults to Talos summary.'),
  to: z.string().email().optional().describe('Recipient email. If omitted, uses SECURITY_ALERT_EMAIL or ALERT_EMAIL.'),
});

const GenerateSecurityReportSchema = z.object({
  target: z.string().optional().describe('Optional website or URL to scan. If omitted, no website scan is run.'),
  product: z.string().optional().describe('Optional product/software name for CVE lookup, for example nginx.'),
  version: z.string().optional().describe('Optional product version for CVE lookup, for example 1.18.0.'),
  research_query: z.string().optional().describe('Optional research query. If omitted and target/product exists, Talos builds a security query.'),
  include_research: z.boolean().optional().describe('Include academic research citations. Defaults to true.'),
  analyze_auth_log: z.boolean().optional().describe('Analyze the bundled auth log too. Defaults to false.'),
  auth_log_path: z.string().optional().describe('Optional auth log path if analyze_auth_log is true.'),
  threshold: z.number().int().positive().optional().describe('Failed-attempt threshold for auth log analysis.'),
  send_email: z.boolean().optional().describe('Only send the report by email when this is explicitly true. Defaults to false.'),
  to: z.string().email().optional().describe('Recipient email, used only when send_email is true.'),
  title: z.string().optional().describe('Optional report title.'),
});

const SendReportEmailSchema = z.object({
  report_id: z.string().optional().describe('Report id from generate_security_report. If omitted, sends the latest report.'),
  to: z.string().email().optional().describe('Recipient email. If omitted, uses SECURITY_ALERT_EMAIL or ALERT_EMAIL.'),
  subject: z.string().optional().describe('Optional email subject.'),
  message: z.string().optional().describe('Optional extra note to prepend before the report.'),
});

const SelfTestAllToolsSchema = z.object({
  include_email: z.boolean().optional().describe('Send a real email as part of the test. Defaults to false to avoid duplicate test messages.'),
  target: z.string().optional().describe('Safe website target for scan tests. Defaults to example.com.'),
});

const PasswordStrengthSchema = z.object({
  password: z.string().describe('Password to analyze.'),
});

const GeneratePasswordSchema = z.object({
  length: z.number().int().min(8).max(128).optional().describe('Password length. Defaults to 20.'),
  symbols: z.boolean().optional().describe('Include symbols. Defaults to true.'),
});

const HashTextSchema = z.object({
  text: z.string().describe('Text to hash.'),
  algo: z.enum(['md5', 'sha1', 'sha256', 'sha512']).optional().describe('Hash algorithm. Defaults to sha256.'),
});

const DecodeJwtSchema = z.object({
  token: z.string().min(1).describe('JWT string to decode without signature verification.'),
});

const LookupIpSchema = z.object({
  ip: z.string().min(1).describe('IPv4 or IPv6 address to investigate.'),
});

const ListSecurityToolsSchema = z.object({
  search: z.string().optional().describe('Keyword query, for example port scan, jwt, whois, password strength.'),
  category: z.string().optional().describe('Optional Talos category filter.'),
});

const RunSecurityToolSchema = z.object({
  name: z.string().min(1).describe('Exact Talos tool name from list_security_tools.'),
  args: z.record(z.unknown()).optional().describe('Keyword arguments for the chosen tool.'),
});

const SearchResourcesSchema = z.object({
  keywords: z.string().min(1).describe('Search keywords or phrase for the local resource library.'),
  limit: z.number().int().min(1).max(20).optional().describe('Maximum result count. Defaults to 20.'),
});

const GetResourcePageSchema = z.object({
  book_id: z.string().min(1).describe('Book id from list_resources or search_resources.'),
  page: z.number().int().positive().describe('One-based page number.'),
});

const EmptySchema = z.object({});

export class TalosTools {
  @Tool({
    name: 'scan_website',
    title: 'Scan Website',
    description: 'Run a safe, non-destructive Talos security scan of an authorized website. Checks HTTPS/TLS, security headers, cookies, exposed files, DNS/email security, and basic fingerprinting. Returns a graded report with remediation.',
    inputSchema: ScanWebsiteSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: true,
    },
    examples: {
      request: { url: 'example.com' },
      response: { target: 'https://example.com', grade: 'A', score: 90, findings: [] },
    },
  })
  async scanWebsite(input: z.infer<typeof ScanWebsiteSchema>, ctx: ExecutionContext) {
    return callTalosTool('scan_website', input, ctx);
  }

  @Tool({
    name: 'lookup_cves',
    title: 'Lookup CVEs',
    description: 'Look up known public vulnerabilities for a software product and optional version, such as nginx 1.18.0.',
    inputSchema: LookupCvesSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: true,
    },
  })
  async lookupCves(input: z.infer<typeof LookupCvesSchema>, ctx: ExecutionContext) {
    return callTalosTool('lookup_cves', input, ctx);
  }

  @Tool({
    name: 'analyze_auth_log',
    title: 'Analyze Auth Log',
    description: 'Analyze an authentication log for brute-force or password-guessing attacks. Reports attacking IPs, attempt counts, targeted usernames, ML confidence, and anomaly flags.',
    inputSchema: AnalyzeAuthLogSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: false,
    },
  })
  async analyzeAuthLog(input: z.infer<typeof AnalyzeAuthLogSchema>, ctx: ExecutionContext) {
    return callTalosTool('analyze_auth_log', input, ctx);
  }

  @Tool({
    name: 'search_research',
    title: 'Search Security Research',
    description: 'Search academic literature across OpenAlex, Semantic Scholar, and CORE. Call this whenever the user asks for research papers, citations, academic papers, studies, references, or literature about a security topic.',
    inputSchema: SearchResearchSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: true,
    },
  })
  async searchResearch(input: z.infer<typeof SearchResearchSchema>, ctx: ExecutionContext) {
    return callTalosTool('search_research', input, ctx);
  }

  @Tool({
    name: 'find_research_papers',
    title: 'Find Research Papers',
    description: 'Find real academic research papers and citations for security scan findings or vulnerability topics. Use this when the user says research papers, papers, citations, studies, academic literature, references, arXiv, Semantic Scholar, OpenAlex, CORE, IEEE-style evidence, or asks for papers about found bugs/findings like HSTS, CSP, X-Frame-Options, clickjacking, MIME sniffing, or Referrer-Policy.',
    inputSchema: FindResearchPapersSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: true,
    },
    examples: {
      request: { findings: ['Missing HSTS', 'Missing Content Security Policy', 'Missing X-Frame-Options'], source: 'openalex' },
      response: { count: 5, papers: [{ title: 'Content Security Problems?', year: 2016 }] },
    },
  })
  async findResearchPapers(input: z.infer<typeof FindResearchPapersSchema>, ctx: ExecutionContext) {
    return callTalosTool('find_research_papers', input, ctx);
  }

  @Tool({
    name: 'analyze_link_safety',
    title: 'Analyze Link Safety',
    description: 'Check whether a URL/link is safe to open or enter information into. Use this whenever the user asks "is this link safe", "is this phishing", "check this URL", "can I open this", "is this scam/malware", "detect phishing link", or shares a suspicious link. Detects phishing, social engineering, brand impersonation, malware, unwanted software, scams, risky downloads, shorteners, suspicious redirects, typosquatting, homograph/punycode, unsafe HTTP, private-network redirects, and provider threat-intel matches. Returns a verdict, risk score, reasons, safe points, and recommendation.',
    inputSchema: AnalyzeLinkSafetySchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: false,
      readOnlyHint: true,
      openWorldHint: true,
    },
    examples: {
      request: { url: 'https://example.com', expected_brand: 'Example', check_providers: true },
      response: { verdict: 'safe', risk_score: 5, categories: [], recommendation: 'No major red flags found.' },
    },
  })
  async analyzeLinkSafety(input: z.infer<typeof AnalyzeLinkSafetySchema>, ctx: ExecutionContext) {
    return callTalosTool('analyze_link_safety', input, ctx);
  }

  @Tool({
    name: 'generate_blocklist',
    title: 'Generate Blocklist',
    description: 'Generate ready-to-review defensive firewall rules for attacking IPs. Does not execute the rules.',
    inputSchema: GenerateBlocklistSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: false,
    },
  })
  async generateBlocklist(input: z.infer<typeof GenerateBlocklistSchema>, ctx: ExecutionContext) {
    return callTalosTool('generate_blocklist', input, ctx);
  }

  @Tool({
    name: 'send_alert',
    title: 'Send Security Alert',
    description: 'Send a security alert to configured Gmail/SMTP email or Slack channels. Use only when the user wants an alert sent.',
    inputSchema: SendAlertSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: false,
      readOnlyHint: false,
      openWorldHint: true,
    },
  })
  async sendAlert(input: z.infer<typeof SendAlertSchema>, ctx: ExecutionContext) {
    return callTalosTool('send_alert', input, ctx);
  }

  @Tool({
    name: 'send_email',
    title: 'Send Email',
    description: 'Forward, email, share, or send information to the user or another recipient using the configured Gmail SMTP sender. Call this when the user says things like "send this to me", "forward these points", "email this report", or gives an email address. If no recipient is provided, sends to the configured SECURITY_ALERT_EMAIL or ALERT_EMAIL.',
    inputSchema: SendEmailSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: false,
      readOnlyHint: false,
      openWorldHint: true,
    },
    examples: {
      request: { subject: 'Talos summary', message: 'Important points...', to: 'user@example.com' },
      response: { sent: true, channel: 'email' },
    },
  })
  async sendEmail(input: z.infer<typeof SendEmailSchema>, ctx: ExecutionContext) {
    return callTalosTool('send_email', input, ctx);
  }

  @Tool({
    name: 'generate_security_report',
    title: 'Generate Security Report',
    description: 'Generate a consolidated Talos security report from website scan, CVEs, research, and optional auth-log analysis. By default this ONLY returns the report. Do not email, forward, or send it unless the user explicitly asks for email/send/forward/share, or send_email is true.',
    inputSchema: GenerateSecurityReportSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: false,
      readOnlyHint: true,
      openWorldHint: true,
    },
  })
  async generateSecurityReport(input: z.infer<typeof GenerateSecurityReportSchema>, ctx: ExecutionContext) {
    return callTalosTool('generate_security_report', input, ctx);
  }

  @Tool({
    name: 'send_report_email',
    title: 'Send Report Email',
    description: 'Email the latest generated Talos report, or a report by id. Use this only when the user explicitly asks to email, send, forward, or share the report.',
    inputSchema: SendReportEmailSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: false,
      readOnlyHint: false,
      openWorldHint: true,
    },
  })
  async sendReportEmail(input: z.infer<typeof SendReportEmailSchema>, ctx: ExecutionContext) {
    return callTalosTool('send_report_email', input, ctx);
  }

  @Tool({
    name: 'self_test_all_tools',
    title: 'Self-Test All Tools',
    description: 'Run a Nitro-compatible health check across Talos tools and runtime assumptions. Does not send email unless include_email is explicitly true.',
    inputSchema: SelfTestAllToolsSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: false,
      readOnlyHint: true,
      openWorldHint: true,
    },
  })
  async selfTestAllTools(input: z.infer<typeof SelfTestAllToolsSchema>, ctx: ExecutionContext) {
    return callTalosTool('self_test_all_tools', input, ctx);
  }

  @Tool({
    name: 'check_password_strength',
    title: 'Check Password Strength',
    description: 'Analyze password strength, entropy, estimated crack time, common issues, and known breach exposure using k-anonymity where configured.',
    inputSchema: PasswordStrengthSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: true,
    },
  })
  async checkPasswordStrength(input: z.infer<typeof PasswordStrengthSchema>, ctx: ExecutionContext) {
    return callTalosTool('check_password_strength', input, ctx);
  }

  @Tool({
    name: 'generate_password',
    title: 'Generate Password',
    description: 'Generate a strong random password.',
    inputSchema: GeneratePasswordSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: false,
      readOnlyHint: true,
      openWorldHint: false,
    },
  })
  async generatePassword(input: z.infer<typeof GeneratePasswordSchema>, ctx: ExecutionContext) {
    return callTalosTool('generate_password', input, ctx);
  }

  @Tool({
    name: 'hash_text',
    title: 'Hash Text',
    description: 'Compute MD5, SHA-1, SHA-256, or SHA-512 hashes of text.',
    inputSchema: HashTextSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: false,
    },
  })
  async hashText(input: z.infer<typeof HashTextSchema>, ctx: ExecutionContext) {
    return callTalosTool('hash_text', input, ctx);
  }

  @Tool({
    name: 'decode_jwt',
    title: 'Decode JWT',
    description: 'Decode a JWT header and payload without signature verification.',
    inputSchema: DecodeJwtSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: false,
    },
  })
  async decodeJwt(input: z.infer<typeof DecodeJwtSchema>, ctx: ExecutionContext) {
    return callTalosTool('decode_jwt', input, ctx);
  }

  @Tool({
    name: 'lookup_ip',
    title: 'Lookup IP',
    description: 'Look up geolocation, ISP, ASN, reverse DNS, and reputation indicators for an IP address.',
    inputSchema: LookupIpSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: true,
    },
  })
  async lookupIp(input: z.infer<typeof LookupIpSchema>, ctx: ExecutionContext) {
    return callTalosTool('lookup_ip', input, ctx);
  }

  @Tool({
    name: 'get_defense_status',
    title: 'Get Defense Status',
    description: 'Get Talos self-defense status: blocked IPs, attack counts, attack type breakdown, and recent events.',
    inputSchema: EmptySchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: false,
    },
  })
  async getDefenseStatus(input: z.infer<typeof EmptySchema>, ctx: ExecutionContext) {
    return callTalosTool('get_defense_status', input, ctx);
  }

  @Tool({
    name: 'list_security_tools',
    title: 'List Security Tools',
    description: 'Search Talos built-in security tools by keyword and optional category. Use this before run_security_tool.',
    inputSchema: ListSecurityToolsSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: false,
    },
  })
  async listSecurityTools(input: z.infer<typeof ListSecurityToolsSchema>, ctx: ExecutionContext) {
    return callTalosTool('list_security_tools', input, ctx);
  }

  @Tool({
    name: 'run_security_tool',
    title: 'Run Security Tool',
    description: 'Run one of Talos built-in security tools by exact name. Discover names with list_security_tools first. Defensive use only on authorized systems.',
    inputSchema: RunSecurityToolSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: false,
      readOnlyHint: false,
      openWorldHint: true,
    },
  })
  async runSecurityTool(input: z.infer<typeof RunSecurityToolSchema>, ctx: ExecutionContext) {
    return callTalosTool('run_security_tool', input, ctx);
  }

  @Tool({
    name: 'search_resources',
    title: 'Search Resource Library',
    description: 'Search the local uploaded resource library by keyword and return matching page snippets with citations.',
    inputSchema: SearchResourcesSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: false,
    },
  })
  async searchResources(input: z.infer<typeof SearchResourcesSchema>, ctx: ExecutionContext) {
    return callTalosTool('search_resources', input, ctx);
  }

  @Tool({
    name: 'get_resource_page',
    title: 'Get Resource Page',
    description: 'Fetch the full text of one page from a book in the local resource library.',
    inputSchema: GetResourcePageSchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: false,
    },
  })
  async getResourcePage(input: z.infer<typeof GetResourcePageSchema>, ctx: ExecutionContext) {
    return callTalosTool('get_resource_page', input, ctx);
  }

  @Tool({
    name: 'list_resources',
    title: 'List Resource Library',
    description: 'List books and documents available in the local Talos resource library.',
    inputSchema: EmptySchema,
    annotations: {
      destructiveHint: false,
      idempotentHint: true,
      readOnlyHint: true,
      openWorldHint: false,
    },
  })
  async listResources(input: z.infer<typeof EmptySchema>, ctx: ExecutionContext) {
    return callTalosTool('list_resources', input, ctx);
  }
}

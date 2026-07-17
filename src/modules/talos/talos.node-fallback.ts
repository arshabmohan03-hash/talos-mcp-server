import { createHash, randomBytes } from 'node:crypto';
import dns from 'node:dns/promises';
import { promises as fs } from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { domainToUnicode } from 'node:url';
import nodemailer from 'nodemailer';

export type JsonObject = Record<string, unknown>;

const FALLBACK_TOOLS = new Set([
  'scan_website',
  'lookup_cves',
  'analyze_auth_log',
  'search_research',
  'find_research_papers',
  'analyze_link_safety',
  'generate_blocklist',
  'send_alert',
  'send_email',
  'generate_security_report',
  'send_report_email',
  'self_test_all_tools',
  'get_last_security_report',
  'check_password_strength',
  'generate_password',
  'hash_text',
  'decode_jwt',
  'lookup_ip',
  'get_defense_status',
  'list_security_tools',
  'run_security_tool',
  'search_resources',
  'get_resource_page',
  'list_resources',
]);

const USER_AGENT = 'Talos-Security/1.0';
const REQUEST_TIMEOUT_MS = 12000;
const RESEARCH_PROVIDER_TIMEOUT_MS = 12000;
const RESOURCE_SEARCH_LIMIT = 20;
const RESOURCE_SNIPPET_CHARS = 700;

const LINK_INTEL_KEY_DEFAULTS = {
  googleSafeBrowsing: '',
  virusTotal: '',
  phishTank: '',
  urlscan: '',
};

const LINK_SAFETY_SHORTENERS = new Set([
  'bit.ly',
  'tinyurl.com',
  't.co',
  'goo.gl',
  'ow.ly',
  'is.gd',
  'buff.ly',
  'rebrand.ly',
  'cutt.ly',
  'shorturl.at',
  'lnkd.in',
  's.id',
  'rb.gy',
  'soo.gd',
  'trib.al',
  'bitly.com',
  'youtu.be',
]);

const LINK_SAFETY_RISKY_TLDS = new Set([
  'zip',
  'mov',
  'click',
  'top',
  'xyz',
  'quest',
  'country',
  'work',
  'gq',
  'tk',
  'cf',
  'ml',
  'ga',
]);

const LINK_SAFETY_DOWNLOAD_EXTENSIONS = new Set([
  '.exe',
  '.scr',
  '.bat',
  '.cmd',
  '.msi',
  '.apk',
  '.dmg',
  '.pkg',
  '.zip',
  '.rar',
  '.7z',
  '.js',
  '.vbs',
  '.ps1',
  '.hta',
  '.jar',
  '.docm',
  '.xlsm',
  '.pptm',
  '.iso',
]);

type ReportStatus = 'ok' | 'warning' | 'error' | 'skipped';

type ReportSection = {
  name: string;
  status: ReportStatus;
  summary: string;
  data?: unknown;
};

type SecurityReport = {
  report_id: string;
  created_at: string;
  title: string;
  target?: string;
  product?: string;
  version?: string;
  summary: {
    status: string;
    sections: number;
    warnings: number;
    errors: number;
    skipped: number;
    headline: string;
  };
  sections: ReportSection[];
  recommendations: string[];
  markdown: string;
  saved?: {
    latest: string;
    report: string;
  };
  email?: unknown;
  fallback: 'node';
};

type SelfTestItem = {
  tool: string;
  status: 'pass' | 'fail' | 'skipped';
  duration_ms: number;
  summary: string;
  detail?: unknown;
};

type LinkSignal = {
  category: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  title: string;
  detail: string;
  score: number;
  evidence?: unknown;
};

type ProviderResult = {
  provider: string;
  status: 'hit' | 'clean' | 'unknown' | 'skipped' | 'error';
  categories?: string[];
  detail?: string;
  evidence?: unknown;
};

const COMMON_PASSWORDS = new Set([
  'password',
  '123456',
  '12345678',
  'qwerty',
  'admin',
  'letmein',
  'welcome',
  'iloveyou',
  '111111',
  '123123',
  'abc123',
  '000000',
  'root',
  'toor',
  'passw0rd',
]);

const SECURITY_TOOL_SPECS = [
  spec('base64_encode', 'Crypto & Encoding', 'Base64-encode text.', ['text']),
  spec('base64_decode', 'Crypto & Encoding', 'Base64-decode text.', ['text']),
  spec('url_encode', 'Crypto & Encoding', 'URL-encode text.', ['text']),
  spec('url_decode', 'Crypto & Encoding', 'URL-decode text.', ['text']),
  spec('hash_text', 'Crypto & Encoding', 'Compute MD5, SHA-1, SHA-256, and SHA-512 hashes.', ['text', 'algo']),
  spec('hash_identifier', 'Crypto & Encoding', 'Identify likely hash algorithms by digest length and characters.', ['hash']),
  spec('jwt_decode', 'Crypto & Encoding', 'Decode a JWT header and payload without verifying the signature.', ['token']),
  spec('password_strength', 'Defensive / Blue-team', 'Estimate password entropy and common weaknesses.', ['password']),
  spec('generate_password', 'Defensive / Blue-team', 'Generate a strong random password.', ['length', 'symbols']),
  spec('dns_lookup', 'Network & Recon', 'Resolve common DNS records for a domain.', ['domain']),
  spec('lookup_ip', 'OSINT & Threat Intel', 'Look up public IP geolocation and ASN information.', ['ip']),
  spec('lookup_cves', 'OSINT & Threat Intel', 'Search NVD for public CVEs by product and optional version.', ['product', 'version']),
  spec('http_headers', 'Web App Testing', 'Fetch response headers for a URL.', ['url']),
  spec('security_headers', 'Web App Testing', 'Check common HTTP security headers for a URL.', ['url']),
  spec('robots_txt', 'Web App Testing', 'Fetch robots.txt for a website.', ['url']),
];

const keyCache = new Map<string, Promise<Record<string, string>>>();

export function canHandleTalosNodeFallback(tool: string): boolean {
  return FALLBACK_TOOLS.has(tool);
}

export async function callTalosNodeFallback(
  tool: string,
  args: JsonObject,
  appRoot: string,
): Promise<unknown> {
  switch (tool) {
    case 'scan_website':
      return scanWebsite(textArg(args, 'url'));
    case 'lookup_cves':
      return lookupCves(textArg(args, 'product'), optionalTextArg(args, 'version'));
    case 'analyze_auth_log':
      return analyzeAuthLog(appRoot, optionalTextArg(args, 'path'), optionalNumberArg(args, 'threshold'));
    case 'search_research':
      return searchResearch(appRoot, args);
    case 'find_research_papers':
      return findResearchPapers(appRoot, args);
    case 'analyze_link_safety':
      return analyzeLinkSafety(appRoot, args);
    case 'generate_blocklist':
      return generateBlocklist(arrayArg(args, 'ips'), optionalNumberArg(args, 'threshold') ?? 5);
    case 'send_alert':
      return sendAlert(appRoot, textArg(args, 'message'), optionalTextArg(args, 'subject') || 'Talos security alert', optionalTextArg(args, 'to'));
    case 'send_email':
      return sendEmail(appRoot, textArg(args, 'message'), optionalTextArg(args, 'subject') || 'Talos summary', optionalTextArg(args, 'to'));
    case 'generate_security_report':
      return generateSecurityReport(appRoot, args);
    case 'send_report_email':
      return sendReportEmail(appRoot, args);
    case 'self_test_all_tools':
      return selfTestAllTools(appRoot, args);
    case 'get_last_security_report':
      return getLastSecurityReport(appRoot);
    case 'check_password_strength':
      return checkPasswordStrength(textArg(args, 'password'));
    case 'generate_password':
      return generatePassword(optionalNumberArg(args, 'length') ?? 20, optionalBooleanArg(args, 'symbols') ?? true);
    case 'hash_text':
      return hashText(textArg(args, 'text'), optionalTextArg(args, 'algo') || 'sha256');
    case 'decode_jwt':
      return decodeJwt(textArg(args, 'token'));
    case 'lookup_ip':
      return lookupIp(textArg(args, 'ip'));
    case 'get_defense_status':
      return getDefenseStatus();
    case 'list_security_tools':
      return listSecurityTools(optionalTextArg(args, 'search'), optionalTextArg(args, 'category'));
    case 'run_security_tool':
      return runSecurityTool(textArg(args, 'name'), objectArg(args, 'args'));
    case 'search_resources':
      return searchResources(appRoot, textArg(args, 'keywords'), optionalNumberArg(args, 'limit') ?? RESOURCE_SEARCH_LIMIT);
    case 'get_resource_page':
      return getResourcePage(appRoot, textArg(args, 'book_id'), optionalNumberArg(args, 'page') ?? 0);
    case 'list_resources':
      return { books: await listBooks(appRoot), fallback: 'node' };
    default:
      return { error: `Unknown tool: ${tool}` };
  }
}

function spec(name: string, category: string, description: string, inputs: string[]) {
  return { name, category, description, inputs, label: name.replaceAll('_', ' ') };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function textArg(args: JsonObject, key: string): string {
  const value = args[key];
  return typeof value === 'string' ? value : value == null ? '' : String(value);
}

function optionalTextArg(args: JsonObject, key: string): string | undefined {
  const value = args[key];
  if (value == null || value === '') {
    return undefined;
  }
  return typeof value === 'string' ? value : String(value);
}

function optionalNumberArg(args: JsonObject, key: string): number | undefined {
  const value = args[key];
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function optionalBooleanArg(args: JsonObject, key: string): boolean | undefined {
  const value = args[key];
  if (typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'string') {
    if (['1', 'true', 'yes'].includes(value.toLowerCase())) {
      return true;
    }
    if (['0', 'false', 'no'].includes(value.toLowerCase())) {
      return false;
    }
  }
  return undefined;
}

function arrayArg(args: JsonObject, key: string): unknown[] {
  const value = args[key];
  return Array.isArray(value) ? value : [];
}

function objectArg(args: JsonObject, key: string): JsonObject {
  const value = args[key];
  return isRecord(value) ? value : {};
}

function withTimeout(timeoutMs = REQUEST_TIMEOUT_MS): AbortController {
  return new AbortController();
}

async function fetchWithTimeout(url: string, init: RequestInit = {}, timeoutMs = REQUEST_TIMEOUT_MS): Promise<Response> {
  const controller = withTimeout(timeoutMs);
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {
      ...init,
      signal: controller.signal,
      headers: {
        'User-Agent': USER_AGENT,
        ...(init.headers || {}),
      },
    });
  } finally {
    clearTimeout(timer);
  }
}

async function fetchJson(url: string, init: RequestInit = {}, timeoutMs = REQUEST_TIMEOUT_MS): Promise<unknown> {
  const response = await fetchWithTimeout(url, init, timeoutMs);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response.json() as Promise<unknown>;
}

function urlWithParams(base: string, params: Record<string, string | number | boolean | undefined>): string {
  const url = new URL(base);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') {
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

async function readLocalTestKeys(appRoot: string): Promise<Record<string, string>> {
  const cached = keyCache.get(appRoot);
  if (cached) {
    return cached;
  }
  const promise = (async () => {
    const file = path.join(appRoot, 'app', 'local_test_keys.py');
    try {
      const raw = await fs.readFile(file, 'utf8');
      const keys: Record<string, string> = {};
      const rx = /"([A-Z0-9_]+)"\s*:\s*("(?:\\.|[^"\\])*")/g;
      let match: RegExpExecArray | null;
      while ((match = rx.exec(raw)) !== null) {
        try {
          keys[match[1]] = JSON.parse(match[2]) as string;
        } catch {
          // Ignore malformed embedded values.
        }
      }
      return keys;
    } catch {
      return {};
    }
  })();
  keyCache.set(appRoot, promise);
  return promise;
}

async function secret(appRoot: string, name: string): Promise<string> {
  const fromEnv = process.env[name];
  if (fromEnv && fromEnv.trim()) {
    return fromEnv.trim();
  }
  const keys = await readLocalTestKeys(appRoot);
  return keys[name] || '';
}

function compactPaper(paper: Record<string, unknown>) {
  const text = String(paper.tldr || paper.abstract || '');
  return {
    title: paper.title,
    authors: Array.isArray(paper.authors) ? paper.authors.slice(0, 4) : [],
    year: paper.year,
    journal: paper.journal,
    cited_by: paper.cited_by,
    is_oa: paper.is_oa,
    url: paper.oa_url || paper.url,
    doi: paper.doi,
    summary: text.length > 240 ? `${text.slice(0, 240)}...` : text,
    source: paper.source,
  };
}

function reconstructOpenAlexAbstract(value: unknown): string {
  if (!isRecord(value)) {
    return '';
  }
  const positions: Array<[number, string]> = [];
  for (const [word, indexes] of Object.entries(value)) {
    if (!Array.isArray(indexes)) {
      continue;
    }
    for (const index of indexes) {
      if (typeof index === 'number') {
        positions.push([index, word]);
      }
    }
  }
  return positions.sort((a, b) => a[0] - b[0]).map(([, word]) => word).join(' ');
}

function normalizeOpenAlex(work: unknown): Record<string, unknown> {
  const w = isRecord(work) ? work : {};
  const openAccess = isRecord(w.open_access) ? w.open_access : {};
  const primaryLocation = isRecord(w.primary_location) ? w.primary_location : {};
  const source = isRecord(primaryLocation.source) ? primaryLocation.source : {};
  const authorships = Array.isArray(w.authorships) ? w.authorships : [];
  return {
    id: w.id,
    doi: typeof w.doi === 'string' ? w.doi.replace('https://doi.org/', '') : undefined,
    title: w.title || w.display_name || 'Untitled',
    year: w.publication_year,
    type: w.type,
    cited_by: w.cited_by_count || 0,
    is_oa: Boolean(openAccess.is_oa),
    oa_url: openAccess.oa_url,
    journal: source.display_name,
    authors: authorships.map((entry) => {
      const author = isRecord(entry) && isRecord(entry.author) ? entry.author : {};
      return author.display_name;
    }).filter(Boolean).slice(0, 10),
    abstract: reconstructOpenAlexAbstract(w.abstract_inverted_index),
    url: w.doi || w.id,
    source: 'OpenAlex',
  };
}

function normalizeSemanticScholar(paper: unknown): Record<string, unknown> {
  const p = isRecord(paper) ? paper : {};
  const externalIds = isRecord(p.externalIds) ? p.externalIds : {};
  const openAccessPdf = isRecord(p.openAccessPdf) ? p.openAccessPdf : {};
  const journal = isRecord(p.journal) ? p.journal : {};
  const tldr = isRecord(p.tldr) ? p.tldr : {};
  const authors = Array.isArray(p.authors) ? p.authors : [];
  return {
    id: p.paperId,
    doi: externalIds.DOI,
    title: p.title || 'Untitled',
    year: p.year,
    cited_by: p.citationCount || 0,
    is_oa: Boolean(p.isOpenAccess || openAccessPdf.url),
    oa_url: openAccessPdf.url,
    journal: p.venue || journal.name,
    authors: authors.map((entry) => isRecord(entry) ? entry.name : undefined).filter(Boolean).slice(0, 10),
    abstract: p.abstract || '',
    tldr: tldr.text,
    url: p.url || (externalIds.DOI ? `https://doi.org/${externalIds.DOI}` : undefined),
    source: 'Semantic Scholar',
  };
}

function normalizeCore(work: unknown): Record<string, unknown> {
  const w = isRecord(work) ? work : {};
  const authors = Array.isArray(w.authors) ? w.authors : [];
  return {
    id: w.id == null ? undefined : String(w.id),
    doi: w.doi,
    title: w.title || 'Untitled',
    year: w.yearPublished,
    type: w.documentType,
    cited_by: w.citationCount || 0,
    is_oa: true,
    oa_url: w.downloadUrl,
    journal: w.publisher,
    authors: authors.map((entry) => isRecord(entry) ? entry.name : undefined).filter(Boolean).slice(0, 10),
    abstract: w.abstract || '',
    url: w.downloadUrl || (w.doi ? `https://doi.org/${w.doi}` : undefined),
    source: 'CORE',
  };
}

async function searchOpenAlex(appRoot: string, query: string, yearFrom?: number, openAccess = false, limit = 8) {
  const filters: string[] = [];
  if (yearFrom) {
    filters.push(`publication_year:>${yearFrom - 1}`);
  }
  if (openAccess) {
    filters.push('is_oa:true');
  }
  const apiKey = await secret(appRoot, 'OPENALEX_API_KEY');
  const data = await fetchJson(urlWithParams('https://api.openalex.org/works', {
    search: query,
    per_page: Math.min(limit, 25),
    sort: 'relevance_score:desc',
    filter: filters.length ? filters.join(',') : undefined,
    api_key: apiKey || undefined,
  }));
  const results = isRecord(data) && Array.isArray(data.results) ? data.results : [];
  return results.map(normalizeOpenAlex);
}

async function searchSemanticScholar(appRoot: string, query: string, yearFrom?: number, openAccess = false, limit = 8) {
  const apiKey = await secret(appRoot, 'SEMANTIC_SCHOLAR_API_KEY');
  const fields = 'title,year,citationCount,authors,isOpenAccess,openAccessPdf,abstract,url,venue,journal,externalIds,tldr';
  const headers = apiKey ? { 'x-api-key': apiKey } : undefined;
  const params: Record<string, string | number | boolean | undefined> = {
    query,
    limit: Math.min(limit, 25),
    fields,
    year: yearFrom ? `${yearFrom}-` : undefined,
    openAccessPdf: openAccess ? '' : undefined,
  };
  let data: unknown;
  try {
    data = await fetchJson(urlWithParams('https://api.semanticscholar.org/graph/v1/paper/search', params), { headers });
  } catch (error) {
    const bulkFields = 'title,year,citationCount,authors,openAccessPdf,abstract,url,venue,externalIds';
    data = await fetchJson(urlWithParams('https://api.semanticscholar.org/graph/v1/paper/search/bulk', {
      query,
      fields: bulkFields,
      sort: 'citationCount:desc',
      year: yearFrom ? `${yearFrom}-` : undefined,
      openAccessPdf: openAccess ? '' : undefined,
    }), { headers });
  }
  const results = isRecord(data) && Array.isArray(data.data) ? data.data : [];
  return results.slice(0, limit).map(normalizeSemanticScholar);
}

async function searchCore(appRoot: string, query: string, yearFrom?: number, limit = 8) {
  const apiKey = await secret(appRoot, 'CORE_API_KEY');
  if (!apiKey) {
    throw new Error('CORE API key not configured');
  }
  const q = yearFrom ? `${query} AND yearPublished>${yearFrom - 1}` : query;
  const data = await fetchJson(urlWithParams('https://api.core.ac.uk/v3/search/works/', {
    q,
    limit: Math.min(limit, 25),
    offset: 0,
  }), { headers: { Authorization: `Bearer ${apiKey}` } });
  const results = isRecord(data) && Array.isArray(data.results) ? data.results : [];
  return results.map(normalizeCore);
}

async function searchResearch(appRoot: string, args: JsonObject) {
  const query = textArg(args, 'query').trim();
  if (!query) {
    return { error: 'Empty research query.' };
  }
  const source = (optionalTextArg(args, 'source') || 'all').toLowerCase();
  const yearFrom = optionalNumberArg(args, 'year_from');
  const openAccess = optionalBooleanArg(args, 'open_access') ?? false;
  const providers = source === 'all' ? ['openalex', 'semantic_scholar', 'core'] : [source];
  const tasks = providers.map(async (provider) => withDeadline(async () => {
    if (provider === 'openalex') {
      return searchOpenAlex(appRoot, query, yearFrom, openAccess);
    }
    if (provider === 'semantic_scholar' || provider === 's2') {
      return searchSemanticScholar(appRoot, query, yearFrom, openAccess);
    }
    if (provider === 'core') {
      return searchCore(appRoot, query, yearFrom);
    }
    throw new Error(`Unknown research source: ${provider}`);
  }, RESEARCH_PROVIDER_TIMEOUT_MS, `${provider} timed out`));
  const settled = await Promise.allSettled(tasks);
  const papers: Record<string, unknown>[] = [];
  const providerErrors: string[] = [];
  for (const result of settled) {
    if (result.status === 'fulfilled') {
      papers.push(...result.value);
    } else {
      providerErrors.push(result.reason instanceof Error ? result.reason.message : String(result.reason));
    }
  }
  const deduped = dedupePapers(papers)
    .sort((a, b) => Number(b.cited_by || 0) - Number(a.cited_by || 0))
    .slice(0, 16);
  return {
    query,
    source,
    providers: Array.from(new Set(deduped.map((paper) => paper.source).filter(Boolean))),
    count: deduped.length,
    papers: deduped.slice(0, 12).map(compactPaper),
    provider_errors: providerErrors.length ? providerErrors : undefined,
    fallback: 'node',
  };
}

function researchFindingTerms(value: string): string[] {
  const text = value.toLowerCase();
  const terms: string[] = [];
  if (text.includes('strict-transport-security') || text.includes('hsts')) {
    terms.push('HTTP Strict Transport Security HSTS web security');
  }
  if (text.includes('content-security-policy') || text.includes('csp')) {
    terms.push('Content Security Policy CSP effectiveness web security');
  }
  if (text.includes('x-frame-options') || text.includes('clickjack') || text.includes('frame-ancestors')) {
    terms.push('clickjacking X-Frame-Options frame-ancestors web security');
  }
  if (text.includes('x-content-type-options') || text.includes('mime sniff')) {
    terms.push('MIME sniffing X-Content-Type-Options browser security');
  }
  if (text.includes('referrer-policy') || text.includes('referer policy')) {
    terms.push('Referrer-Policy privacy web security');
  }
  if (text.includes('cookie') && text.includes('httponly')) {
    terms.push('HttpOnly cookies session security');
  }
  if (text.includes('cookie') && text.includes('secure')) {
    terms.push('Secure cookies HTTPS session security');
  }
  if (text.includes('brute') || text.includes('failed password')) {
    terms.push('brute force login detection authentication security');
  }
  return terms;
}

function buildResearchPaperQuery(args: JsonObject): { query: string; findingTopics: string[] } {
  const direct = optionalTextArg(args, 'query') || optionalTextArg(args, 'topic');
  const findings = arrayArg(args, 'findings').map((value) => String(value).trim()).filter(Boolean);
  const target = optionalTextArg(args, 'target');
  const product = optionalTextArg(args, 'product');
  const findingTopics = Array.from(new Set(findings.flatMap(researchFindingTerms)));

  if (direct) {
    return { query: direct, findingTopics };
  }

  if (findingTopics.length) {
    return {
      query: `${findingTopics.join(' OR ')} research papers`,
      findingTopics,
    };
  }

  if (findings.length) {
    return {
      query: `${findings.slice(0, 6).join(' ')} web security research papers`,
      findingTopics,
    };
  }

  if (product) {
    return {
      query: `${product} ${optionalTextArg(args, 'version') || ''} security vulnerability research papers`.trim(),
      findingTopics,
    };
  }

  if (target) {
    return {
      query: `${target} website security headers hardening research papers`,
      findingTopics,
    };
  }

  return { query: '', findingTopics };
}

async function findResearchPapers(appRoot: string, args: JsonObject) {
  const { query, findingTopics } = buildResearchPaperQuery(args);
  if (!query) {
    return {
      error: 'Provide `query`, `topic`, `findings`, `target`, or `product` to search for research papers.',
      examples: [
        { findings: ['Missing HSTS', 'Missing Content Security Policy'] },
        { topic: 'Content Security Policy effectiveness' },
      ],
      fallback: 'node',
    };
  }

  const source = optionalTextArg(args, 'source') || 'all';
  const yearFrom = optionalNumberArg(args, 'year_from') ?? 2010;
  const openAccess = optionalBooleanArg(args, 'open_access') ?? false;
  const maxResults = Math.max(1, Math.min(optionalNumberArg(args, 'max_results') ?? 12, 16));
  const result = await searchResearch(appRoot, {
    query,
    source,
    year_from: yearFrom,
    open_access: openAccess,
  });

  if (isRecord(result) && Array.isArray(result.papers)) {
    result.papers = result.papers.slice(0, maxResults);
    result.count = Math.min(Number(result.count || result.papers.length), result.papers.length);
  }

  return {
    ...(isRecord(result) ? result : { result }),
    tool_intent: 'research_papers',
    generated_query: query,
    finding_topics: findingTopics,
    selection_hint: 'Use this tool whenever the user asks for research papers, citations, academic literature, references, studies, or papers about scan findings.',
    fallback: 'node',
  };
}

async function withDeadline<T>(fn: () => Promise<T>, timeoutMs: number, message: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      fn(),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(message)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) {
      clearTimeout(timer);
    }
  }
}

function dedupePapers(papers: Record<string, unknown>[]) {
  const seen = new Set<string>();
  const out: Record<string, unknown>[] = [];
  for (const paper of papers) {
    const key = String(paper.doi || paper.title || paper.id || '').trim().toLowerCase();
    if (!key || seen.has(key)) {
      continue;
    }
    seen.add(key);
    out.push(paper);
  }
  return out;
}

async function listBooks(appRoot: string) {
  const root = path.join(appRoot, 'resources');
  try {
    const entries = await fs.readdir(root, { withFileTypes: true });
    const books = await Promise.all(entries.filter((entry) => entry.isDirectory()).map(async (entry) => {
      try {
        const raw = await fs.readFile(path.join(root, entry.name, 'meta.json'), 'utf8');
        const meta = JSON.parse(raw) as Record<string, unknown>;
        return {
          book_id: meta.book_id || entry.name,
          title: meta.title || entry.name,
          pages: meta.pages || 0,
          paragraphs: meta.paragraphs || 0,
          bytes: meta.bytes || 0,
          ext: meta.ext,
          created_at: meta.created_at || 0,
          ocr_pages: meta.ocr_pages || 0,
        };
      } catch {
        return undefined;
      }
    }));
    return books
      .filter((book): book is NonNullable<typeof book> => Boolean(book))
      .sort((a, b) => Number(b.created_at) - Number(a.created_at));
  } catch {
    return [];
  }
}

function safeBookId(bookId: string): string | undefined {
  const trimmed = bookId.trim();
  if (!trimmed || trimmed.includes('/') || trimmed.includes('\\') || trimmed.includes('..')) {
    return undefined;
  }
  return trimmed;
}

async function loadPages(appRoot: string, bookId: string): Promise<string[] | undefined> {
  const safe = safeBookId(bookId);
  if (!safe) {
    return undefined;
  }
  const dir = path.join(appRoot, 'resources', safe);
  try {
    const raw = await fs.readFile(path.join(dir, 'pages.json'), 'utf8');
    const pages = JSON.parse(raw) as unknown;
    return Array.isArray(pages) ? pages.map((page) => String(page || '')) : undefined;
  } catch {
    try {
      const raw = await fs.readFile(path.join(dir, 'book.md'), 'utf8');
      return splitMarkdownPages(raw);
    } catch {
      return undefined;
    }
  }
}

async function loadMeta(appRoot: string, bookId: string): Promise<Record<string, unknown>> {
  const safe = safeBookId(bookId);
  if (!safe) {
    return {};
  }
  try {
    const raw = await fs.readFile(path.join(appRoot, 'resources', safe, 'meta.json'), 'utf8');
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function splitMarkdownPages(markdown: string): string[] {
  const matches = Array.from(markdown.matchAll(/^[ \t]*##[ \t]*PAGE[ \t]*NO[ \t]*(\d+)[ \t]*$/gim));
  if (!matches.length) {
    return markdown.trim() ? [markdown.trim()] : [];
  }
  return matches.map((match, index) => {
    const start = (match.index || 0) + match[0].length;
    const end = index + 1 < matches.length ? matches[index + 1].index || markdown.length : markdown.length;
    return markdown.slice(start, end).trim();
  });
}

function tokens(text: string): string[] {
  return (text.toLowerCase().match(/[a-z0-9]+/g) || []);
}

function splitParagraphs(text: string): string[] {
  return text
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .replace(/-\n(\w)/g, '$1')
    .split(/\n\s*\n/g)
    .map((block) => block.replace(/\s+/g, ' ').trim())
    .filter((block) => block.length >= 25);
}

function snippet(text: string, terms: string[], width = RESOURCE_SNIPPET_CHARS): string {
  const clean = text.replace(/\s+/g, ' ').trim();
  if (clean.length <= width) {
    return clean;
  }
  const lower = clean.toLowerCase();
  const hit = terms.map((term) => lower.indexOf(term)).filter((pos) => pos >= 0).sort((a, b) => a - b)[0] ?? 0;
  const start = Math.max(0, hit - Math.floor(width / 3));
  const end = Math.min(clean.length, start + width);
  return `${start > 0 ? '...' : ''}${clean.slice(start, end).trim()}${end < clean.length ? '...' : ''}`;
}

async function searchResources(appRoot: string, keywords: string, limit: number) {
  const query = keywords.trim();
  const terms = Array.from(new Set(tokens(query)));
  if (!terms.length) {
    return { error: 'Provide one or more keywords to search.' };
  }
  const books = await listBooks(appRoot);
  const records: Array<{ book_id: string; book_title: string; page: number; para_idx: number; text: string }> = [];
  for (const book of books) {
    const bookId = String(book.book_id || '');
    const pages = await loadPages(appRoot, bookId);
    if (!pages) {
      continue;
    }
    pages.forEach((pageText, pageIndex) => {
      splitParagraphs(pageText).forEach((para, paraIndex) => {
        records.push({
          book_id: bookId,
          book_title: String(book.title || bookId),
          page: pageIndex + 1,
          para_idx: paraIndex,
          text: para,
        });
      });
    });
  }
  const scored = records.map((record) => {
    const lower = record.text.toLowerCase();
    const hitCount = terms.reduce((sum, term) => sum + (lower.includes(term) ? 1 : 0), 0);
    if (!hitCount) {
      return undefined;
    }
    const score = hitCount * 10 + terms.reduce((sum, term) => sum + countOccurrences(lower, term), 0);
    return { score, record };
  }).filter((entry): entry is { score: number; record: typeof records[number] } => Boolean(entry));
  scored.sort((a, b) => b.score - a.score || a.record.book_title.localeCompare(b.record.book_title) || a.record.page - b.record.page);
  const cap = Math.max(1, Math.min(limit || RESOURCE_SEARCH_LIMIT, RESOURCE_SEARCH_LIMIT));
  const results = scored.slice(0, cap).map(({ score, record }) => ({
    book_id: record.book_id,
    book_title: record.book_title,
    page: record.page,
    para_idx: record.para_idx,
    score,
    snippet: snippet(record.text, terms),
  }));
  return {
    query,
    match_count: scored.length,
    showing: results.length,
    results,
    note: scored.length > results.length
      ? `Top ${results.length} of ${scored.length}; call get_resource_page(book_id, page) for a full page.`
      : scored.length ? null : 'No matching paragraphs. Try different keywords, or call list_resources.',
    fallback: 'node',
  };
}

function countOccurrences(text: string, term: string): number {
  let count = 0;
  let offset = 0;
  while (term && (offset = text.indexOf(term, offset)) !== -1) {
    count += 1;
    offset += term.length;
  }
  return count;
}

async function getResourcePage(appRoot: string, bookId: string, page: number) {
  const pages = await loadPages(appRoot, bookId);
  if (!pages) {
    return { error: `Book not found: '${bookId}'. Use list_resources to see ids.` };
  }
  const pageNumber = Math.trunc(page);
  if (pageNumber < 1 || pageNumber > pages.length) {
    return { error: `Page ${pageNumber} out of range (1-${pages.length}).` };
  }
  const meta = await loadMeta(appRoot, bookId);
  const text = pages[pageNumber - 1];
  return {
    book_id: bookId,
    book_title: meta.title || bookId,
    page: pageNumber,
    page_count: pages.length,
    text,
    chars: text.length,
    fallback: 'node',
  };
}

function hashText(text: string, algo = 'sha256') {
  const algorithms = ['md5', 'sha1', 'sha256', 'sha512'];
  const all = Object.fromEntries(algorithms.map((name) => [name, createHash(name).update(text).digest('hex')]));
  const selected = algorithms.includes(algo.toLowerCase()) ? algo.toLowerCase() : 'sha256';
  return {
    input_length: text.length,
    algorithm: selected,
    hash: all[selected],
    all,
    fallback: 'node',
  };
}

function decodeBase64UrlJson(segment: string): unknown | null {
  const normalized = segment.replace(/-/g, '+').replace(/_/g, '/');
  const padded = normalized + '='.repeat((4 - (normalized.length % 4)) % 4);
  try {
    return JSON.parse(Buffer.from(padded, 'base64').toString('utf8')) as unknown;
  } catch {
    return null;
  }
}

function decodeJwt(token: string) {
  const parts = token.trim().split('.');
  if (parts.length < 2) {
    return { error: 'Not a JWT; expected header.payload.signature.' };
  }
  return {
    header: decodeBase64UrlJson(parts[0]),
    payload: decodeBase64UrlJson(parts[1]),
    signature_present: parts.length >= 3 && Boolean(parts[2]),
    note: 'Signature is NOT verified; this is decode-only.',
    fallback: 'node',
  };
}

function entropyForPassword(password: string): { entropy: number; classes: number; pool: number } {
  const pool = (/[a-z]/.test(password) ? 26 : 0)
    + (/[A-Z]/.test(password) ? 26 : 0)
    + (/\d/.test(password) ? 10 : 0)
    + (/[^\w]/.test(password) ? 33 : 0);
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^\w]/].filter((rx) => rx.test(password)).length;
  const entropy = pool ? Math.round(password.length * Math.log2(pool) * 10) / 10 : 0;
  return { entropy, classes, pool };
}

function humanTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds > 13_800_000_000 * 31536000) {
    return 'longer than the age of the universe';
  }
  if (seconds < 1) {
    return 'instantly';
  }
  for (const [unit, value] of [['years', 31536000], ['days', 86400], ['hours', 3600], ['minutes', 60]] as const) {
    if (seconds >= value) {
      return `~${Math.round(seconds / value).toLocaleString()} ${unit}`;
    }
  }
  return `~${Math.round(seconds)} seconds`;
}

async function pwnedCount(password: string): Promise<number> {
  if (!password) {
    return 0;
  }
  const sha1 = createHash('sha1').update(password).digest('hex').toUpperCase();
  const prefix = sha1.slice(0, 5);
  const suffix = sha1.slice(5);
  try {
    const response = await fetchWithTimeout(`https://api.pwnedpasswords.com/range/${prefix}`, {}, 8000);
    if (!response.ok) {
      return -1;
    }
    const body = await response.text();
    for (const line of body.split(/\r?\n/)) {
      const [hashSuffix, count] = line.split(':');
      if (hashSuffix === suffix) {
        return Number.parseInt(count, 10) || 0;
      }
    }
    return 0;
  } catch {
    return -1;
  }
}

async function checkPasswordStrength(password: string) {
  const { entropy, classes, pool } = entropyForPassword(password);
  const issues: string[] = [];
  if (password.length < 12) {
    issues.push('Too short; use at least 12 characters.');
  }
  if (classes < 3) {
    issues.push('Mix uppercase, lowercase, digits and symbols.');
  }
  if (COMMON_PASSWORDS.has(password.toLowerCase())) {
    issues.push('This is one of the most common passwords.');
  }
  if (/(.)\1\1/.test(password)) {
    issues.push('Avoid 3+ repeated characters in a row.');
  }
  const pwned = await pwnedCount(password);
  if (pwned > 0) {
    issues.push(`Found in ${pwned.toLocaleString()} known data breaches; do not use it.`);
  }
  const strength = entropy < 28 ? 'very weak' : entropy < 40 ? 'weak' : entropy < 60 ? 'fair' : entropy < 100 ? 'strong' : 'very strong';
  const guesses = pool ? Math.pow(pool, Math.min(password.length, 80)) : 0;
  return {
    length: password.length,
    entropy_bits: entropy,
    score: Math.min(100, Math.trunc(entropy / 1.28)),
    strength,
    char_classes: classes,
    crack_time_offline_fast_hardware: pool ? humanTime(guesses / 1e10) : 'instantly',
    pwned_count: pwned,
    issues: issues.length ? issues : ['Looks solid; strong and not seen in breaches.'],
    fallback: 'node',
  };
}

function generatePassword(length: number, symbols = true) {
  const safeLength = Math.max(8, Math.min(128, Math.trunc(length || 20)));
  const alphabet = `abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789${symbols ? '!@#$%^&*()-_=+[]{}?' : ''}`;
  const bytes = randomBytes(safeLength);
  let password = '';
  for (const byte of bytes) {
    password += alphabet[byte % alphabet.length];
  }
  const { entropy } = entropyForPassword(password);
  const strength = entropy < 28 ? 'very weak' : entropy < 40 ? 'weak' : entropy < 60 ? 'fair' : entropy < 100 ? 'strong' : 'very strong';
  return { password, length: safeLength, entropy_bits: entropy, strength, fallback: 'node' };
}

function isValidIpv4(ip: string): boolean {
  return net.isIP(ip) === 4;
}

function generateBlocklist(values: unknown[], threshold: number) {
  const seen = new Set<string>();
  const ips = values.map((value) => String(value).trim()).filter((ip) => isValidIpv4(ip) && !seen.has(ip) && seen.add(ip));
  if (!ips.length) {
    return { error: 'No valid IPv4 addresses to block.', ip_count: 0, ips: [] };
  }
  return {
    ip_count: ips.length,
    ips,
    fail2ban_jail: [
      '# /etc/fail2ban/jail.local - generated by Talos',
      '[sshd-talos]',
      'enabled  = true',
      'port     = ssh',
      'filter   = sshd',
      'logpath  = %(sshd_log)s',
      `maxretry = ${Math.trunc(threshold || 5)}`,
      'findtime = 600',
      'bantime  = 3600',
    ].join('\n'),
    fail2ban_commands: ips.map((ip) => `fail2ban-client set sshd-talos banip ${ip}`).join('\n'),
    iptables: ips.map((ip) => `iptables -A INPUT -s ${ip} -j DROP`).join('\n'),
    ufw: ips.map((ip) => `ufw deny from ${ip}`).join('\n'),
    windows_firewall: ips.map((ip) => `netsh advfirewall firewall add rule name="Talos block ${ip}" dir=in action=block remoteip=${ip}`).join('\n'),
    blocklist: ips.join('\n'),
    note: 'Review before applying. Talos generates these rules but never runs them for you.',
    fallback: 'node',
  };
}

function cleanTarget(raw: string): string {
  let target = raw.trim();
  if (!target) {
    return '';
  }
  if (target.includes('://')) {
    try {
      target = new URL(target).hostname;
    } catch {
      return '';
    }
  }
  target = target.split('@').pop() || target;
  target = target.split('/')[0].split('?')[0].split('#')[0].trim();
  if (target.startsWith('[')) {
    return target.slice(1).split(']')[0].trim();
  }
  if (target.includes(':') && target.split(':').length === 2 && /^\d+$/.test(target.split(':')[1])) {
    target = target.split(':')[0];
  }
  return target.replace(/\.$/, '');
}

function isPrivateAddress(ip: string): boolean {
  if (net.isIP(ip) === 4) {
    const parts = ip.split('.').map(Number);
    return parts[0] === 10
      || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31)
      || (parts[0] === 192 && parts[1] === 168)
      || parts[0] === 127
      || parts[0] === 169 && parts[1] === 254
      || parts[0] === 0;
  }
  if (net.isIP(ip) === 6) {
    return ip === '::1' || ip.toLowerCase().startsWith('fc') || ip.toLowerCase().startsWith('fd') || ip.toLowerCase().startsWith('fe80');
  }
  return false;
}

async function lookupIp(input: string) {
  const target = cleanTarget(input);
  if (!target) {
    return { error: 'Enter an IP address or domain, for example 8.8.8.8 or example.com.' };
  }
  let ip = target;
  if (!net.isIP(ip)) {
    try {
      const resolved = await dns.lookup(target);
      ip = resolved.address;
    } catch {
      return { error: `Could not resolve '${target}'.`, ip: target };
    }
  }
  if (isPrivateAddress(ip)) {
    return {
      ip,
      note: 'Private, loopback, or reserved address; public geolocation is not available.',
      fallback: 'node',
    };
  }
  const providers = [
    async () => {
      const data = await fetchJson(`https://ipwho.is/${encodeURIComponent(ip)}`);
      if (!isRecord(data) || data.success === false) {
        return undefined;
      }
      const connection = isRecord(data.connection) ? data.connection : {};
      return {
        ip: data.ip || ip,
        country: data.country,
        region: data.region,
        city: data.city,
        isp: connection.isp || connection.org,
        org: connection.org,
        asn: connection.asn,
        reverse_dns: undefined,
        is_proxy_or_vpn: undefined,
        is_hosting_datacenter: undefined,
        source: 'ipwho.is',
        fallback: 'node',
      };
    },
    async () => {
      const data = await fetchJson(`https://ipapi.co/${encodeURIComponent(ip)}/json/`);
      if (!isRecord(data) || data.error) {
        return undefined;
      }
      return {
        ip: data.ip || ip,
        country: data.country_name,
        region: data.region,
        city: data.city,
        isp: data.org,
        org: data.org,
        asn: data.asn,
        reverse_dns: undefined,
        is_proxy_or_vpn: undefined,
        is_hosting_datacenter: undefined,
        source: 'ipapi.co',
        fallback: 'node',
      };
    },
  ];
  const errors: string[] = [];
  for (const provider of providers) {
    try {
      const result = await provider();
      if (result) {
        return result;
      }
      errors.push('no data');
    } catch (error) {
      errors.push(error instanceof Error ? error.message : String(error));
    }
  }
  return { error: `Could not geolocate '${target}' (${errors.join('; ')}).`, ip, fallback: 'node' };
}

async function lookupCves(product: string, version?: string) {
  const query = `${product} ${version || ''}`.trim();
  if (!query) {
    return { error: 'Product is required.' };
  }
  try {
    const cveId = query.match(/\bCVE-\d{4}-\d{4,}\b/i)?.[0]?.toUpperCase();
    const first = await fetchNvdCves(cveId ? { cveId } : { keywordSearch: query, resultsPerPage: 10 });
    let vulnerabilities = first;
    let effectiveQuery = cveId || query;
    let note: string | undefined;
    if (!vulnerabilities.length && version) {
      vulnerabilities = await fetchNvdCves({ keywordSearch: product, resultsPerPage: 10 });
      effectiveQuery = product;
      note = `No exact NVD keyword results for '${query}'. Showing product-level results for '${product}' instead.`;
    }
    const cves = vulnerabilities.map((entry) => {
      const cve = isRecord(entry) && isRecord(entry.cve) ? entry.cve : {};
      const descriptions = Array.isArray(cve.descriptions) ? cve.descriptions : [];
      const english = descriptions.find((desc) => isRecord(desc) && desc.lang === 'en');
      const metrics = isRecord(cve.metrics) ? cve.metrics : {};
      const cvssMetricV31 = Array.isArray(metrics.cvssMetricV31) ? metrics.cvssMetricV31[0] : undefined;
      const cvssMetricV30 = Array.isArray(metrics.cvssMetricV30) ? metrics.cvssMetricV30[0] : undefined;
      const cvssMetricV2 = Array.isArray(metrics.cvssMetricV2) ? metrics.cvssMetricV2[0] : undefined;
      const metric = isRecord(cvssMetricV31) ? cvssMetricV31 : isRecord(cvssMetricV30) ? cvssMetricV30 : isRecord(cvssMetricV2) ? cvssMetricV2 : {};
      const cvssData = isRecord(metric.cvssData) ? metric.cvssData : {};
      return {
        id: cve.id,
        published: cve.published,
        last_modified: cve.lastModified,
        severity: metric.baseSeverity,
        cvss: cvssData.baseScore,
        description: isRecord(english) ? english.value : undefined,
        url: cve.id ? `https://nvd.nist.gov/vuln/detail/${cve.id}` : undefined,
      };
    });
    return { product, version, query: effectiveQuery, count: cves.length, cves, source: 'NVD', note, fallback: 'node' };
  } catch (error) {
    return { error: `CVE lookup failed: ${error instanceof Error ? error.message : String(error)}`, product, version, fallback: 'node' };
  }
}

async function fetchNvdCves(params: Record<string, string | number | boolean | undefined>): Promise<unknown[]> {
  const data = await fetchJson(urlWithParams('https://services.nvd.nist.gov/rest/json/cves/2.0', params), {}, 15000);
  return isRecord(data) && Array.isArray(data.vulnerabilities) ? data.vulnerabilities : [];
}

function normalizeWebsiteUrl(raw: string): URL | undefined {
  const text = raw.trim();
  if (!text) {
    return undefined;
  }
  try {
    const url = new URL(text.includes('://') ? text : `https://${text}`);
    if (!url.hostname) {
      return undefined;
    }
    return url;
  } catch {
    return undefined;
  }
}

function normalizeLinkUrl(raw: string): { url?: URL; normalized?: string; error?: string; original: string } {
  const original = raw;
  const text = raw.trim().replace(/^[<("'`]+|[>)"'`]+$/g, '').replace(/[\u200B-\u200D\uFEFF]/g, '');
  if (!text) {
    return { original, error: 'Empty URL.' };
  }
  try {
    const url = new URL(text.includes('://') ? text : `https://${text}`);
    if (!['http:', 'https:'].includes(url.protocol)) {
      return { original, error: `Unsupported URL scheme: ${url.protocol}` };
    }
    if (!url.hostname) {
      return { original, error: 'URL does not include a hostname.' };
    }
    return { original, url, normalized: url.toString() };
  } catch {
    return { original, error: 'Invalid URL format.' };
  }
}

function rootDomain(hostname: string): string {
  const labels = hostname.toLowerCase().replace(/\.$/, '').split('.').filter(Boolean);
  if (labels.length <= 2) {
    return labels.join('.');
  }
  return labels.slice(-2).join('.');
}

function domainCore(hostname: string): string {
  const domain = rootDomain(hostname);
  return domain.split('.')[0] || domain;
}

function normalizeBrand(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, '');
}

function addSignal(signals: LinkSignal[], signal: LinkSignal) {
  signals.push(signal);
}

function linkStaticSignals(url: URL, original: string, expectedBrand?: string): LinkSignal[] {
  const signals: LinkSignal[] = [];
  const hostname = url.hostname.toLowerCase();
  const labels = hostname.split('.').filter(Boolean);
  const tld = labels.at(-1) || '';
  const pathname = decodeURIComponentSafe(url.pathname).toLowerCase();
  const search = decodeURIComponentSafe(url.search).toLowerCase();
  const decodedRemainder = decodeURIComponentSafe(`${url.pathname}${url.search}${url.hash}`).toLowerCase();
  const unicodeHost = domainToUnicode(hostname);

  if (url.username || url.password || /@/.test(original.replace(/^https?:\/\//i, ''))) {
    addSignal(signals, {
      category: 'url_obfuscation',
      severity: 'high',
      title: 'URL contains user-info or @ obfuscation',
      detail: 'Attackers often hide the real destination after an @ symbol, making the link look like a trusted domain.',
      score: 28,
      evidence: { username_present: Boolean(url.username), password_present: Boolean(url.password) },
    });
  }

  if (hostname.includes('xn--') || unicodeHost !== hostname) {
    addSignal(signals, {
      category: 'homograph_or_punycode',
      severity: 'high',
      title: 'Punycode or lookalike Unicode hostname',
      detail: 'The domain uses encoded international characters that can visually imitate trusted brands.',
      score: 24,
      evidence: { hostname, unicode_hostname: unicodeHost },
    });
  }

  if (net.isIP(hostname)) {
    addSignal(signals, {
      category: 'suspicious_infrastructure',
      severity: isPrivateAddress(hostname) ? 'critical' : 'medium',
      title: isPrivateAddress(hostname) ? 'Link points to private/local IP address' : 'Link points directly to an IP address',
      detail: isPrivateAddress(hostname)
        ? 'Links to private or local IP ranges can target internal services or router/admin pages.'
        : 'Legitimate public services usually use recognizable domain names instead of raw IP links.',
      score: isPrivateAddress(hostname) ? 45 : 14,
      evidence: { hostname },
    });
  }

  if (url.protocol === 'http:') {
    addSignal(signals, {
      category: 'insecure_http',
      severity: 'medium',
      title: 'Uses plain HTTP',
      detail: 'Information entered on this link can be intercepted or modified in transit.',
      score: 18,
    });
  }

  if (url.port && !['80', '443'].includes(url.port)) {
    addSignal(signals, {
      category: 'suspicious_infrastructure',
      severity: 'medium',
      title: 'Uses an unusual port',
      detail: 'Unexpected ports can indicate a non-standard service or phishing infrastructure.',
      score: 10,
      evidence: { port: url.port },
    });
  }

  if (LINK_SAFETY_SHORTENERS.has(hostname) || LINK_SAFETY_SHORTENERS.has(rootDomain(hostname))) {
    addSignal(signals, {
      category: 'url_shortener_or_redirector',
      severity: 'medium',
      title: 'URL shortener or redirector',
      detail: 'Shortened links hide the final destination until they are expanded.',
      score: 15,
      evidence: { hostname },
    });
  }

  if (labels.length >= 5 && !net.isIP(hostname)) {
    addSignal(signals, {
      category: 'url_obfuscation',
      severity: 'low',
      title: 'Many subdomains',
      detail: 'Long subdomain chains can be used to push the real registered domain out of sight.',
      score: 7,
      evidence: { labels: labels.length },
    });
  }

  if (LINK_SAFETY_RISKY_TLDS.has(tld)) {
    addSignal(signals, {
      category: 'suspicious_infrastructure',
      severity: 'low',
      title: 'Higher-risk top-level domain',
      detail: 'This top-level domain is commonly abused in low-cost phishing or scam campaigns. This is not proof by itself.',
      score: 8,
      evidence: { tld },
    });
  }

  const extension = LINK_SAFETY_DOWNLOAD_EXTENSIONS.has(path.extname(pathname)) ? path.extname(pathname) : '';
  if (extension) {
    addSignal(signals, {
      category: 'download_risk',
      severity: ['.exe', '.scr', '.bat', '.cmd', '.ps1', '.vbs', '.hta', '.apk'].includes(extension) ? 'high' : 'medium',
      title: 'Direct download or executable-looking file',
      detail: 'The path ends in a file type that can carry malware, scripts, or risky macro content.',
      score: ['.exe', '.scr', '.bat', '.cmd', '.ps1', '.vbs', '.hta', '.apk'].includes(extension) ? 28 : 16,
      evidence: { extension },
    });
  }

  if (/https?:%2f%2f|https?:\/\/|%40/.test(decodedRemainder)) {
    addSignal(signals, {
      category: 'url_obfuscation',
      severity: 'medium',
      title: 'Nested or encoded URL content',
      detail: 'The link includes another encoded URL or encoded @ symbol, often used in redirect or tracking abuse.',
      score: 14,
    });
  }

  const suspiciousWords = ['login', 'verify', 'secure', 'account', 'update', 'password', 'wallet', 'crypto', 'bank', 'free', 'prize', 'urgent', 'gift', 'invoice', 'payment', 'support'];
  const matchedWords = suspiciousWords.filter((word) => hostname.includes(word) || pathname.includes(word) || search.includes(word));
  if (matchedWords.length >= 2) {
    addSignal(signals, {
      category: 'social_engineering',
      severity: 'medium',
      title: 'Social-engineering keywords in URL',
      detail: 'The URL contains multiple words commonly used in phishing or scam lures.',
      score: 14,
      evidence: { matched_words: matchedWords.slice(0, 8) },
    });
  }

  if (expectedBrand) {
    const brand = normalizeBrand(expectedBrand);
    const hostCore = normalizeBrand(domainCore(hostname));
    const hostAll = normalizeBrand(hostname);
    if (brand && hostAll.includes(brand) && hostCore !== brand) {
      addSignal(signals, {
        category: 'brand_impersonation',
        severity: 'high',
        title: 'Possible brand impersonation',
        detail: `The hostname contains "${expectedBrand}" but the registered domain does not appear to be exactly that brand.`,
        score: 26,
        evidence: { expected_brand: expectedBrand, hostname, registered_domain: rootDomain(hostname) },
      });
    } else if (brand && !hostAll.includes(brand) && decodedRemainder.includes(brand)) {
      addSignal(signals, {
        category: 'brand_impersonation',
        severity: 'medium',
        title: 'Brand appears outside the hostname',
        detail: 'The brand appears in the path or query instead of the registered domain, which can be misleading.',
        score: 16,
        evidence: { expected_brand: expectedBrand, hostname },
      });
    }
  }

  return signals;
}

function decodeURIComponentSafe(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

async function resolveHostForLink(hostname: string): Promise<{ addresses: string[]; signals: LinkSignal[]; error?: string }> {
  if (net.isIP(hostname)) {
    return { addresses: [hostname], signals: [] };
  }
  try {
    const records = await dns.lookup(hostname, { all: true });
    const addresses = records.map((record) => record.address);
    const privateAddresses = addresses.filter(isPrivateAddress);
    const signals: LinkSignal[] = [];
    if (privateAddresses.length) {
      signals.push({
        category: 'private_or_local_network_risk',
        severity: 'critical',
        title: 'Domain resolves to private/local address',
        detail: 'The link resolves to an internal/private IP range. Opening it may target local network services.',
        score: 45,
        evidence: { private_addresses: privateAddresses },
      });
    }
    return { addresses, signals };
  } catch (error) {
    return {
      addresses: [],
      signals: [{
        category: 'suspicious_infrastructure',
        severity: 'medium',
        title: 'DNS lookup failed',
        detail: 'The hostname did not resolve during analysis. Newly registered, expired, or blocked phishing domains often fail intermittently.',
        score: 12,
      }],
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

async function followLinkRedirects(startUrl: URL, enabled: boolean) {
  const chain: Array<Record<string, unknown>> = [];
  const signals: LinkSignal[] = [];
  let current = new URL(startUrl.toString());
  if (!enabled) {
    return { finalUrl: current, chain, signals, skipped: true };
  }

  for (let i = 0; i < 6; i += 1) {
    let response: Response;
    try {
      response = await fetchWithTimeout(current.toString(), { method: 'HEAD', redirect: 'manual' }, 10000);
    } catch {
      response = await fetchWithTimeout(current.toString(), { method: 'GET', redirect: 'manual' }, 10000);
    }
    const location = response.headers.get('location');
    chain.push({
      url: current.toString(),
      status_code: response.status,
      location,
    });
    if (!location || response.status < 300 || response.status >= 400) {
      break;
    }
    const next = new URL(location, current);
    if (!['http:', 'https:'].includes(next.protocol)) {
      signals.push({
        category: 'suspicious_redirect_chain',
        severity: 'high',
        title: 'Redirects to unsupported scheme',
        detail: 'The redirect chain points to a non-web scheme, which may trigger an external application or unsafe handler.',
        score: 24,
        evidence: { scheme: next.protocol },
      });
      break;
    }
    if (rootDomain(next.hostname) !== rootDomain(current.hostname)) {
      signals.push({
        category: 'suspicious_redirect_chain',
        severity: LINK_SAFETY_SHORTENERS.has(current.hostname) ? 'low' : 'medium',
        title: 'Cross-domain redirect',
        detail: 'The link redirects to a different registered domain.',
        score: LINK_SAFETY_SHORTENERS.has(current.hostname) ? 6 : 14,
        evidence: { from: current.hostname, to: next.hostname },
      });
    }
    current = next;
  }

  if (chain.length >= 6) {
    signals.push({
      category: 'suspicious_redirect_chain',
      severity: 'medium',
      title: 'Long redirect chain',
      detail: 'Many redirects make destination verification harder and are common in tracking or evasion.',
      score: 12,
      evidence: { hops: chain.length },
    });
  }

  return { finalUrl: current, chain, signals, skipped: false };
}

async function inspectLinkHttp(url: URL) {
  const signals: LinkSignal[] = [];
  const safePoints: string[] = [];
  try {
    const response = await fetchWithTimeout(url.toString(), { method: 'GET', redirect: 'manual' }, 12000);
    const contentType = response.headers.get('content-type') || '';
    const contentDisposition = response.headers.get('content-disposition') || '';
    const headers = Object.fromEntries(response.headers.entries());
    if (response.ok) {
      safePoints.push(`Final URL responded with HTTP ${response.status}.`);
    }
    if (url.protocol === 'https:') {
      safePoints.push('Final URL uses HTTPS.');
    }
    if (/attachment/i.test(contentDisposition)) {
      signals.push({
        category: 'download_risk',
        severity: 'medium',
        title: 'Response triggers a download',
        detail: 'The server sets Content-Disposition as attachment.',
        score: 16,
        evidence: { content_disposition: contentDisposition },
      });
    }
    if (/application\/(octet-stream|x-msdownload|java-archive)|application\/zip|application\/x-7z|application\/vnd\.android/i.test(contentType)) {
      signals.push({
        category: 'download_risk',
        severity: 'high',
        title: 'Risky downloadable content type',
        detail: 'The final URL serves a file/content type commonly associated with executable or archive downloads.',
        score: 24,
        evidence: { content_type: contentType },
      });
    }

    let body = '';
    if (/text\/html|application\/xhtml/i.test(contentType)) {
      body = (await response.text()).slice(0, 120000);
      if (/<input[^>]+type=["']?password/i.test(body)) {
        signals.push({
          category: url.protocol === 'http:' ? 'credential_harvesting' : 'credential_page',
          severity: url.protocol === 'http:' ? 'high' : 'medium',
          title: 'Page contains a password field',
          detail: url.protocol === 'http:'
            ? 'The page asks for credentials over plain HTTP.'
            : 'The page asks for credentials. Confirm the domain is exactly the intended service before entering anything.',
          score: url.protocol === 'http:' ? 32 : 14,
        });
      }
      if (/<meta[^>]+http-equiv=["']?refresh/i.test(body)) {
        signals.push({
          category: 'suspicious_redirect_chain',
          severity: 'medium',
          title: 'HTML meta refresh redirect',
          detail: 'The page contains a client-side redirect, which can hide the final destination from simple checks.',
          score: 12,
        });
      }
    }

    return {
      status_code: response.status,
      content_type: contentType,
      content_disposition: contentDisposition || undefined,
      headers,
      signals,
      safePoints,
    };
  } catch (error) {
    return {
      error: error instanceof Error ? error.message : String(error),
      signals: [{
        category: 'unknown',
        severity: 'low',
        title: 'Could not fetch final URL',
        detail: 'The link could not be fetched during analysis. This can happen with blocking, downtime, or network controls.',
        score: 5,
      }] satisfies LinkSignal[],
      safePoints,
    };
  }
}

function linkIntelKey(name: keyof typeof LINK_INTEL_KEY_DEFAULTS, envName: string): string {
  return process.env[envName]?.trim() || LINK_INTEL_KEY_DEFAULTS[name];
}

async function checkGoogleSafeBrowsing(url: string): Promise<ProviderResult> {
  const key = linkIntelKey('googleSafeBrowsing', 'GOOGLE_SAFE_BROWSING_API_KEY');
  if (!key) {
    return { provider: 'google_safe_browsing', status: 'skipped', detail: 'API key not configured.' };
  }
  try {
    const data = await fetchJson(`https://safebrowsing.googleapis.com/v4/threatMatches:find?key=${encodeURIComponent(key)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client: { clientId: 'talos-mcp-server', clientVersion: '1.0.0' },
        threatInfo: {
          threatTypes: ['MALWARE', 'SOCIAL_ENGINEERING', 'UNWANTED_SOFTWARE', 'POTENTIALLY_HARMFUL_APPLICATION'],
          platformTypes: ['ANY_PLATFORM'],
          threatEntryTypes: ['URL'],
          threatEntries: [{ url }],
        },
      }),
    }, 12000);
    const matches = isRecord(data) && Array.isArray(data.matches) ? data.matches : [];
    if (matches.length) {
      return {
        provider: 'google_safe_browsing',
        status: 'hit',
        categories: matches.map((match) => isRecord(match) ? String(match.threatType || 'unsafe') : 'unsafe'),
        detail: 'Google Safe Browsing matched this URL.',
        evidence: { match_count: matches.length },
      };
    }
    return { provider: 'google_safe_browsing', status: 'clean', detail: 'No Safe Browsing match.' };
  } catch (error) {
    return { provider: 'google_safe_browsing', status: 'error', detail: error instanceof Error ? error.message : String(error) };
  }
}

function virusTotalUrlId(url: string): string {
  return Buffer.from(url).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

async function checkVirusTotal(url: string): Promise<ProviderResult> {
  const key = linkIntelKey('virusTotal', 'VIRUSTOTAL_API_KEY');
  if (!key) {
    return { provider: 'virustotal', status: 'skipped', detail: 'API key not configured.' };
  }
  try {
    const data = await fetchJson(`https://www.virustotal.com/api/v3/urls/${virusTotalUrlId(url)}`, {
      headers: { 'x-apikey': key },
    }, 12000);
    const attrs = isRecord(data) && isRecord(data.data) && isRecord(data.data.attributes) ? data.data.attributes : {};
    const stats = isRecord(attrs.last_analysis_stats) ? attrs.last_analysis_stats : {};
    const malicious = Number(stats.malicious || 0);
    const suspicious = Number(stats.suspicious || 0);
    const harmless = Number(stats.harmless || 0);
    const undetected = Number(stats.undetected || 0);
    if (malicious || suspicious) {
      return {
        provider: 'virustotal',
        status: 'hit',
        categories: malicious ? ['malware_or_phishing'] : ['suspicious'],
        detail: `${malicious} malicious and ${suspicious} suspicious VirusTotal engine(s) flagged this URL.`,
        evidence: { malicious, suspicious, harmless, undetected, reputation: attrs.reputation },
      };
    }
    return {
      provider: 'virustotal',
      status: harmless || undetected ? 'clean' : 'unknown',
      detail: harmless || undetected ? 'No VirusTotal engine flagged this URL.' : 'VirusTotal has no useful analysis for this URL.',
      evidence: { malicious, suspicious, harmless, undetected, reputation: attrs.reputation },
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { provider: 'virustotal', status: message.includes('404') ? 'unknown' : 'error', detail: message.includes('404') ? 'URL not found in VirusTotal dataset.' : message };
  }
}

async function checkUrlhaus(url: string): Promise<ProviderResult> {
  try {
    const response = await fetchWithTimeout('https://urlhaus-api.abuse.ch/v1/url/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ url }).toString(),
    }, 12000);
    if (response.status === 401 || response.status === 403) {
      return { provider: 'urlhaus', status: 'skipped', detail: `URLhaus lookup rejected unauthenticated request (HTTP ${response.status}).` };
    }
    if (!response.ok) {
      return { provider: 'urlhaus', status: 'error', detail: `HTTP ${response.status}` };
    }
    const data = await response.json() as unknown;
    const queryStatus = isRecord(data) ? String(data.query_status || '') : '';
    if (queryStatus === 'ok') {
      return {
        provider: 'urlhaus',
        status: 'hit',
        categories: ['malware'],
        detail: 'URLhaus has this URL in its malware URL database.',
        evidence: {
          url_status: isRecord(data) ? data.url_status : undefined,
          threat: isRecord(data) ? data.threat : undefined,
          tags: isRecord(data) ? data.tags : undefined,
        },
      };
    }
    return { provider: 'urlhaus', status: 'clean', detail: 'URLhaus has no malware URL match.' };
  } catch (error) {
    return { provider: 'urlhaus', status: 'error', detail: error instanceof Error ? error.message : String(error) };
  }
}

async function checkPhishTank(url: string): Promise<ProviderResult> {
  const key = linkIntelKey('phishTank', 'PHISHTANK_API_KEY');
  if (!key) {
    return { provider: 'phishtank', status: 'skipped', detail: 'PhishTank API key currently unavailable.' };
  }
  try {
    const response = await fetchWithTimeout('https://checkurl.phishtank.com/checkurl/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ url, format: 'json', app_key: key }).toString(),
    }, 12000);
    if (!response.ok) {
      return { provider: 'phishtank', status: 'error', detail: `HTTP ${response.status}` };
    }
    const data = await response.json() as unknown;
    const results = isRecord(data) && isRecord(data.results) ? data.results : {};
    if (results.in_database && results.valid) {
      return {
        provider: 'phishtank',
        status: 'hit',
        categories: ['phishing'],
        detail: 'PhishTank lists this as a valid phishing URL.',
        evidence: { phish_id: results.phish_id, verified: results.verified },
      };
    }
    return { provider: 'phishtank', status: 'clean', detail: 'No valid PhishTank match.' };
  } catch (error) {
    return { provider: 'phishtank', status: 'error', detail: error instanceof Error ? error.message : String(error) };
  }
}

async function checkUrlscan(url: string, submitToSandbox: boolean): Promise<ProviderResult> {
  const key = linkIntelKey('urlscan', 'URLSCAN_API_KEY');
  if (!key) {
    return { provider: 'urlscan', status: 'skipped', detail: 'API key not configured.' };
  }
  try {
    if (submitToSandbox) {
      const response = await fetchWithTimeout('https://urlscan.io/api/v1/scan/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'API-Key': key },
        body: JSON.stringify({ url, visibility: 'unlisted' }),
      }, 15000);
      if (!response.ok) {
        return { provider: 'urlscan', status: 'error', detail: `Scan submission HTTP ${response.status}` };
      }
      const data = await response.json() as unknown;
      return {
        provider: 'urlscan',
        status: 'unknown',
        detail: 'URL submitted to urlscan as unlisted. Review the returned result URL after processing.',
        evidence: isRecord(data) ? { uuid: data.uuid, result: data.result, visibility: 'unlisted' } : undefined,
      };
    }

    const data = await fetchJson(urlWithParams('https://urlscan.io/api/v1/search/', {
      q: `page.url:"${url.replaceAll('"', '')}"`,
      size: 10,
    }), { headers: { 'API-Key': key } }, 12000);
    const results = isRecord(data) && Array.isArray(data.results) ? data.results : [];
    let malicious = 0;
    let phishing = 0;
    for (const entry of results) {
      const verdicts = isRecord(entry) && isRecord(entry.verdicts) ? entry.verdicts : {};
      const overall = isRecord(verdicts.overall) ? verdicts.overall : {};
      if (overall.malicious) {
        malicious += 1;
      }
      if (overall.hasVerdicts && Array.isArray(overall.categories) && overall.categories.some((cat) => String(cat).toLowerCase().includes('phish'))) {
        phishing += 1;
      }
    }
    if (malicious || phishing) {
      return {
        provider: 'urlscan',
        status: 'hit',
        categories: phishing ? ['phishing'] : ['malicious'],
        detail: `urlscan public results include ${malicious} malicious and ${phishing} phishing-related result(s).`,
        evidence: { result_count: results.length, malicious, phishing },
      };
    }
    return {
      provider: 'urlscan',
      status: results.length ? 'clean' : 'unknown',
      detail: results.length ? 'No malicious urlscan public result found.' : 'No public urlscan result found. Not submitted because submit_to_sandbox is false.',
      evidence: { result_count: results.length },
    };
  } catch (error) {
    return { provider: 'urlscan', status: 'error', detail: error instanceof Error ? error.message : String(error) };
  }
}

async function runLinkProviders(url: string, enabled: boolean, privacyMode: boolean, submitToSandbox: boolean): Promise<ProviderResult[]> {
  if (!enabled) {
    return [{ provider: 'all', status: 'skipped', detail: 'Provider checks disabled by input.' }];
  }
  if (privacyMode) {
    return [{ provider: 'all', status: 'skipped', detail: 'Privacy mode is enabled, so external provider checks were not run.' }];
  }
  return Promise.all([
    checkGoogleSafeBrowsing(url),
    checkVirusTotal(url),
    checkUrlhaus(url),
    checkPhishTank(url),
    checkUrlscan(url, submitToSandbox),
  ]);
}

function providerSignals(providerResults: ProviderResult[]): LinkSignal[] {
  return providerResults.flatMap((provider) => {
    if (provider.status !== 'hit') {
      return [];
    }
    const category = provider.categories?.[0]?.toLowerCase() || 'provider_threat_match';
    return [{
      category,
      severity: 'critical' as const,
      title: `${provider.provider} threat-intel match`,
      detail: provider.detail || `${provider.provider} flagged this URL.`,
      score: category.includes('phish') || category.includes('social') ? 48 : 55,
      evidence: provider.evidence,
    }];
  });
}

function linkVerdict(score: number, signals: LinkSignal[], providers: ProviderResult[]) {
  const critical = signals.some((signal) => signal.severity === 'critical');
  const providerHit = providers.some((provider) => provider.status === 'hit');
  if (providerHit || critical || score >= 70) {
    return 'unsafe';
  }
  if (score >= 30 || signals.some((signal) => signal.severity === 'high')) {
    return 'suspicious';
  }
  if (providers.some((provider) => provider.status === 'error') && !providers.some((provider) => provider.status === 'clean')) {
    return 'unknown';
  }
  return 'safe';
}

function linkConfidence(verdict: string, providers: ProviderResult[], fetched: boolean) {
  if (providers.some((provider) => provider.status === 'hit')) {
    return 'high';
  }
  const cleanProviders = providers.filter((provider) => provider.status === 'clean').length;
  if (verdict === 'safe' && cleanProviders >= 2 && fetched) {
    return 'high';
  }
  if (cleanProviders || fetched) {
    return 'medium';
  }
  return 'low';
}

function linkRecommendation(verdict: string) {
  if (verdict === 'unsafe') {
    return 'Do not open this link or enter credentials/payment details. Report it and use the official site/app by typing the address yourself.';
  }
  if (verdict === 'suspicious') {
    return 'Treat this link as suspicious. Do not enter passwords, payment data, recovery codes, or personal information unless you verify the domain through an official source.';
  }
  if (verdict === 'unknown') {
    return 'Talos could not gather enough evidence. Open only in a protected browser/session and avoid entering sensitive information.';
  }
  return 'No major red flags were found. Still verify the address bar before entering sensitive information, especially if the link came from email, SMS, ads, QR codes, or chat.';
}

function safeLinkPoints(url: URL, addresses: string[], providers: ProviderResult[], signals: LinkSignal[], httpSafePoints: string[]) {
  const points = [...httpSafePoints];
  if (url.protocol === 'https:' && !points.includes('Final URL uses HTTPS.')) {
    points.push('Uses HTTPS.');
  }
  if (addresses.length && !addresses.some(isPrivateAddress)) {
    points.push('DNS resolved to public IP address(es), not private/local ranges.');
  }
  const clean = providers.filter((provider) => provider.status === 'clean').map((provider) => provider.provider);
  if (clean.length) {
    points.push(`No threat match from: ${clean.join(', ')}.`);
  }
  if (!signals.some((signal) => ['homograph_or_punycode', 'url_obfuscation', 'brand_impersonation'].includes(signal.category))) {
    points.push('No obvious URL obfuscation, punycode, or brand-impersonation pattern was detected.');
  }
  return Array.from(new Set(points));
}

async function analyzeLinkSafety(appRoot: string, args: JsonObject) {
  void appRoot;
  const normalized = normalizeLinkUrl(textArg(args, 'url'));
  if (!normalized.url) {
    return {
      verdict: 'unknown',
      risk_score: 100,
      confidence: 'high',
      categories: ['invalid_url'],
      recommendation: 'Do not open it until the URL is corrected and can be analyzed.',
      reasons: [normalized.error || 'Invalid URL.'],
      safe_points: [],
      evidence: { original_url: normalized.original },
      user_explanation: `Talos could not analyze the link because it is not a valid HTTP/HTTPS URL: ${normalized.error || 'invalid URL'}`,
      fallback: 'node',
    };
  }

  const originalUrl = normalized.url;
  const followRedirects = optionalBooleanArg(args, 'follow_redirects') ?? true;
  const checkProviders = optionalBooleanArg(args, 'check_providers') ?? true;
  const privacyMode = optionalBooleanArg(args, 'privacy_mode') ?? false;
  const submitToSandbox = optionalBooleanArg(args, 'submit_to_sandbox') === true;
  const expectedBrand = optionalTextArg(args, 'expected_brand');
  const context = optionalTextArg(args, 'context');

  const staticSignals = linkStaticSignals(originalUrl, normalized.original, expectedBrand);
  const redirect = await followLinkRedirects(originalUrl, followRedirects);
  const redirectChoiceSignals: LinkSignal[] = [];
  if (!followRedirects && (LINK_SAFETY_SHORTENERS.has(originalUrl.hostname.toLowerCase()) || LINK_SAFETY_SHORTENERS.has(rootDomain(originalUrl.hostname)))) {
    redirectChoiceSignals.push({
      category: 'url_shortener_or_redirector',
      severity: 'medium',
      title: 'Shortened link was not expanded',
      detail: 'The link uses a shortener and redirects were disabled, so the true destination was not verified.',
      score: 16,
      evidence: { hostname: originalUrl.hostname },
    });
  }
  const finalUrl = redirect.finalUrl;
  const finalStaticSignals = finalUrl.toString() === originalUrl.toString()
    ? []
    : linkStaticSignals(finalUrl, finalUrl.toString(), expectedBrand).map((signal) => ({ ...signal, title: `Final URL: ${signal.title}` }));
  const dnsResult = await resolveHostForLink(finalUrl.hostname);
  const httpResult = await inspectLinkHttp(finalUrl);
  const providerResults = await runLinkProviders(finalUrl.toString(), checkProviders, privacyMode, submitToSandbox);
  const signals: LinkSignal[] = [
    ...staticSignals,
    ...redirect.signals,
    ...redirectChoiceSignals,
    ...finalStaticSignals,
    ...dnsResult.signals,
    ...httpResult.signals,
    ...providerSignals(providerResults),
  ];
  const rawScore = signals.reduce((sum, signal) => sum + signal.score, 0);
  const riskScore = Math.max(0, Math.min(100, rawScore));
  const verdict = linkVerdict(riskScore, signals, providerResults);
  const confidence = linkConfidence(verdict, providerResults, !httpResult.error);
  const categories = Array.from(new Set(signals.map((signal) => signal.category)));
  const reasons = signals
    .sort((a, b) => b.score - a.score)
    .map((signal) => `${signal.title}: ${signal.detail}`);
  const safePoints = safeLinkPoints(finalUrl, dnsResult.addresses, providerResults, signals, httpResult.safePoints);
  const recommendation = linkRecommendation(verdict);

  return {
    verdict,
    risk_score: riskScore,
    confidence,
    categories,
    recommendation,
    reasons: reasons.length ? reasons : ['No major phishing, malware, redirect, or obfuscation red flags were detected by the completed checks.'],
    safe_points: safePoints,
    evidence: {
      original_url: normalized.original,
      normalized_url: originalUrl.toString(),
      final_url: finalUrl.toString(),
      context,
      expected_brand: expectedBrand,
      redirect_chain: redirect.chain,
      dns: {
        hostname: finalUrl.hostname,
        addresses: dnsResult.addresses,
        error: dnsResult.error,
      },
      http: {
        status_code: httpResult.status_code,
        content_type: httpResult.content_type,
        content_disposition: httpResult.content_disposition,
        error: httpResult.error,
      },
      provider_results: providerResults,
      static_signals: signals.map((signal) => ({
        category: signal.category,
        severity: signal.severity,
        title: signal.title,
        detail: signal.detail,
        evidence: signal.evidence,
      })),
      options: {
        follow_redirects: followRedirects,
        check_providers: checkProviders,
        privacy_mode: privacyMode,
        submit_to_sandbox: submitToSandbox,
      },
    },
    user_explanation: `${verdict.toUpperCase()} (${confidence} confidence, risk ${riskScore}/100): ${recommendation}`,
    note: submitToSandbox
      ? 'Sandbox submission was enabled; this can expose the URL to the scanning provider.'
      : 'Sandbox submission was not used. Provider checks use lookups where possible; urlscan submission requires submit_to_sandbox=true.',
    fallback: 'node',
  };
}

function securityHeaderFindings(url: URL, headers: Headers) {
  const findings: Array<Record<string, unknown>> = [];
  const missing = (name: string, severity: 'low' | 'medium' | 'high', title: string, remediation: string) => {
    if (!headers.get(name)) {
      findings.push({
        severity,
        title,
        description: `${name} is not present in the response.`,
        evidence: `Missing header: ${name}`,
        remediation,
      });
    }
  };
  if (url.protocol !== 'https:') {
    findings.push({
      severity: 'high',
      title: 'HTTP is not encrypted',
      description: 'The target was fetched over plaintext HTTP.',
      evidence: url.toString(),
      remediation: 'Serve the site over HTTPS and redirect HTTP to HTTPS.',
    });
  }
  missing('strict-transport-security', 'medium', 'Missing HSTS', 'Add Strict-Transport-Security after HTTPS is stable.');
  missing('content-security-policy', 'medium', 'Missing Content Security Policy', 'Add a Content-Security-Policy tuned to the application.');
  missing('x-frame-options', 'low', 'Missing clickjacking protection', 'Add X-Frame-Options or frame-ancestors in CSP.');
  missing('x-content-type-options', 'low', 'Missing MIME sniffing protection', 'Add X-Content-Type-Options: nosniff.');
  missing('referrer-policy', 'low', 'Missing Referrer-Policy', 'Add a Referrer-Policy appropriate for the app.');
  return findings;
}

function gradeFromScore(score: number): string {
  return score >= 90 ? 'A' : score >= 80 ? 'B' : score >= 70 ? 'C' : score >= 60 ? 'D' : 'F';
}

async function scanWebsite(rawUrl: string) {
  const url = normalizeWebsiteUrl(rawUrl);
  if (!url) {
    return { error: 'Provide a valid website or URL.' };
  }
  try {
    const response = await fetchWithTimeout(url.toString(), { method: 'GET', redirect: 'follow' }, 15000);
    const headers = response.headers;
    const findings = securityHeaderFindings(url, headers);
    const setCookie = headers.get('set-cookie') || '';
    if (setCookie && !/;\s*httponly/i.test(setCookie)) {
      findings.push({
        severity: 'low',
        title: 'Cookie missing HttpOnly',
        description: 'At least one Set-Cookie header did not include HttpOnly.',
        evidence: 'Set-Cookie without HttpOnly',
        remediation: 'Set HttpOnly on session cookies that do not need JavaScript access.',
      });
    }
    if (setCookie && url.protocol === 'https:' && !/;\s*secure/i.test(setCookie)) {
      findings.push({
        severity: 'medium',
        title: 'Cookie missing Secure',
        description: 'At least one Set-Cookie header did not include Secure.',
        evidence: 'Set-Cookie without Secure',
        remediation: 'Set Secure on cookies served over HTTPS.',
      });
    }
    const score = Math.max(0, 100 - findings.reduce((sum, finding) => {
      const severity = finding.severity;
      return sum + (severity === 'high' ? 18 : severity === 'medium' ? 10 : 4);
    }, 0));
    return {
      target: url.toString(),
      status_code: response.status,
      grade: gradeFromScore(score),
      score,
      findings,
      headers: Object.fromEntries(headers.entries()),
      next_tools: findings.length ? [{
        tool: 'find_research_papers',
        when: 'If the user asks for research papers, citations, academic literature, references, or studies about these findings.',
        arguments: {
          findings: findings.map((finding) => String(finding.title || finding.description || '')).filter(Boolean),
          target: url.hostname,
          source: 'all',
          year_from: 2010,
        },
      }] : [],
      note: 'Node fallback scan: passive HTTP/header checks only. Full Python scan adds DNS, TLS certificate, and deeper fingerprinting.',
      fallback: 'node',
    };
  } catch (error) {
    return { error: `Website scan failed: ${error instanceof Error ? error.message : String(error)}`, target: url.toString(), fallback: 'node' };
  }
}

async function analyzeAuthLog(appRoot: string, inputPath?: string, threshold = 5) {
  const logPath = inputPath
    ? (path.isAbsolute(inputPath) ? inputPath : path.resolve(appRoot, inputPath))
    : path.join(appRoot, 'auth.log');
  try {
    const raw = await fs.readFile(logPath, 'utf8');
    const counts = new Map<string, number>();
    const users = new Map<string, Set<string>>();
    const rx = /Failed password(?: for (?:invalid user )?(\S+))? from (\d{1,3}(?:\.\d{1,3}){3})/g;
    let failed = 0;
    let match: RegExpExecArray | null;
    while ((match = rx.exec(raw)) !== null) {
      failed += 1;
      const user = match[1] || '';
      const ip = match[2];
      counts.set(ip, (counts.get(ip) || 0) + 1);
      if (!users.has(ip)) {
        users.set(ip, new Set());
      }
      if (user) {
        users.get(ip)?.add(user);
      }
    }
    const results = Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).map(([ip, attempts]) => {
      const userSet = users.get(ip) || new Set<string>();
      const status = attempts >= threshold + 1 ? 'attack' : attempts >= Math.max(1, threshold - 1) ? 'suspicious' : 'normal';
      return {
        ip,
        attempts,
        status,
        unique_users: userSet.size,
        targeted_usernames: Array.from(userSet).slice(0, 8),
        ai_confidence: null,
        anomaly: null,
      };
    });
    return {
      summary: {
        total_failed_logins: failed,
        unique_ips: counts.size,
        attacks: results.filter((result) => result.status === 'attack').length,
        suspicious: results.filter((result) => result.status === 'suspicious').length,
        anomalies: 0,
      },
      results,
      threshold,
      log_path: logPath,
      note: 'Node fallback uses heuristic auth-log parsing. Full ML scoring requires the Python runtime.',
      fallback: 'node',
    };
  } catch {
    return {
      error: `Log file not found: ${logPath}`,
      note: 'Upload an auth.log into the standalone folder or run the full Python app for live log analysis.',
      fallback: 'node',
    };
  }
}

async function mailConfig(appRoot: string) {
  const from = await secret(appRoot, 'ALERT_EMAIL');
  const password = await secret(appRoot, 'ALERT_EMAIL_PASSWORD');
  const host = await secret(appRoot, 'SMTP_HOST') || 'smtp.gmail.com';
  const rawPort = await secret(appRoot, 'SMTP_PORT') || '587';
  const port = Number.parseInt(rawPort, 10) || 587;
  const defaultTo = await secret(appRoot, 'SECURITY_ALERT_EMAIL') || from;
  return { from, password, host, port, defaultTo };
}

function cleanEmailList(value?: string): string | undefined {
  const cleaned = (value || '').split(',').map((part) => part.trim()).filter(Boolean).join(', ');
  return cleaned || undefined;
}

async function sendEmailMessage(appRoot: string, message: string, subject: string, to?: string) {
  const cfg = await mailConfig(appRoot);
  const recipient = cleanEmailList(to) || cleanEmailList(cfg.defaultTo);
  if (!recipient) {
    return {
      sent: false,
      error: 'No recipient email provided. Pass `to` or configure SECURITY_ALERT_EMAIL.',
      configured: Boolean(cfg.from && cfg.password),
      fallback: 'node',
    };
  }
  if (!cfg.from || !cfg.password) {
    return {
      sent: false,
      error: 'Gmail SMTP is not configured. Provide ALERT_EMAIL and ALERT_EMAIL_PASSWORD, or run npm run embed:test-keys for this test deployment.',
      to: recipient,
      configured: false,
      fallback: 'node',
    };
  }

  try {
    const transporter = nodemailer.createTransport({
      host: cfg.host,
      port: cfg.port,
      secure: cfg.port === 465,
      auth: {
        user: cfg.from,
        pass: cfg.password,
      },
    });
    const info = await transporter.sendMail({
      from: `Talos <${cfg.from}>`,
      to: recipient,
      subject,
      text: message,
    });
    return {
      sent: true,
      channel: 'email',
      to: recipient,
      from: cfg.from,
      subject,
      message_id: info.messageId,
      configured: true,
      fallback: 'node',
    };
  } catch (error) {
    return {
      sent: false,
      error: error instanceof Error ? error.message : String(error),
      to: recipient,
      from: cfg.from,
      configured: true,
      fallback: 'node',
    };
  }
}

async function sendEmail(appRoot: string, message: string, subject: string, to?: string) {
  return sendEmailMessage(appRoot, message, subject, to);
}

async function sendAlert(appRoot: string, message: string, subject: string, to?: string) {
  const channels: Record<string, string> = {};
  const configuredChannels: string[] = [];
  let sentAny = false;

  const email = await sendEmailMessage(appRoot, message, subject, to);
  if (email.configured) {
    configuredChannels.push('email');
  }
  channels.email = email.sent ? 'sent' : email.error || 'not configured';
  sentAny = sentAny || Boolean(email.sent);

  const webhook = await secret(appRoot, 'SLACK_WEBHOOK_URL');
  if (webhook) {
    configuredChannels.push('slack');
    try {
      const response = await fetchWithTimeout(webhook, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: `*${subject}*\n${message}` }),
      }, 12000);
      channels.slack = response.ok ? 'sent' : `HTTP ${response.status}`;
      sentAny = sentAny || response.ok;
    } catch (error) {
      channels.slack = error instanceof Error ? error.message : String(error);
    }
  } else {
    channels.slack = 'not configured';
  }

  return {
    sent: sentAny,
    channels,
    configured_channels: configuredChannels,
    email: {
      sent: email.sent,
      to: email.to,
      from: email.from,
      error: email.error,
    },
    note: sentAny ? '' : 'No alert was sent. Check Gmail SMTP credentials, recipient, or Slack webhook configuration.',
    fallback: 'node',
  };
}

function reportsDir(appRoot: string): string {
  return path.join(appRoot, 'reports');
}

function reportTimestampId(date = new Date()): string {
  return `security-report-${date.toISOString().replace(/[:.]/g, '-')}`;
}

function safeReportId(reportId: string): string {
  return reportId.trim().replace(/[^a-zA-Z0-9_.-]/g, '');
}

function toolError(result: unknown): string | undefined {
  if (isRecord(result) && result.error) {
    return String(result.error);
  }
  return undefined;
}

function sectionStatus(result: unknown, warning: boolean): ReportStatus {
  if (toolError(result)) {
    return 'error';
  }
  return warning ? 'warning' : 'ok';
}

function resultArray(record: unknown, key: string): unknown[] {
  return isRecord(record) && Array.isArray(record[key]) ? record[key] : [];
}

function resultNumber(record: unknown, key: string): number {
  if (!isRecord(record)) {
    return 0;
  }
  const value = record[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function resultText(record: unknown, key: string): string | undefined {
  if (!isRecord(record)) {
    return undefined;
  }
  const value = record[key];
  return typeof value === 'string' && value.trim() ? value : undefined;
}

function summarizeWebsiteScan(scan: unknown): string {
  const error = toolError(scan);
  if (error) {
    return `Website scan failed: ${error}`;
  }
  const grade = resultText(scan, 'grade') || 'unknown';
  const score = resultNumber(scan, 'score');
  const findings = resultArray(scan, 'findings');
  return `Website scan grade ${grade}, score ${score}, with ${findings.length} finding(s).`;
}

function summarizeCves(cves: unknown): string {
  const error = toolError(cves);
  if (error) {
    return `CVE lookup failed: ${error}`;
  }
  return `Found ${resultNumber(cves, 'count')} public CVE result(s).`;
}

function summarizeResearch(research: unknown): string {
  const error = toolError(research);
  if (error) {
    return `Research search failed: ${error}`;
  }
  const providers = resultArray(research, 'providers').map((entry) => String(entry)).join(', ') || 'no providers';
  return `Found ${resultNumber(research, 'count')} paper(s) from ${providers}.`;
}

function summarizeAuthLog(authLog: unknown): string {
  const error = toolError(authLog);
  if (error) {
    return `Auth log analysis failed: ${error}`;
  }
  const summary = isRecord(authLog) && isRecord(authLog.summary) ? authLog.summary : {};
  const failed = resultNumber(summary, 'total_failed_logins');
  const attacks = resultNumber(summary, 'attacks');
  const suspicious = resultNumber(summary, 'suspicious');
  return `Auth log has ${failed} failed login(s), ${attacks} attack IP(s), and ${suspicious} suspicious IP(s).`;
}

function websiteRecommendations(scan: unknown): string[] {
  const findings = resultArray(scan, 'findings');
  const recs = findings.map((finding) => {
    if (!isRecord(finding)) {
      return undefined;
    }
    const title = resultText(finding, 'title') || 'Website finding';
    const remediation = resultText(finding, 'remediation');
    return remediation ? `${title}: ${remediation}` : title;
  }).filter((entry): entry is string => Boolean(entry));
  return recs.slice(0, 6);
}

function cveRecommendations(cves: unknown, product?: string, version?: string): string[] {
  if (toolError(cves) || resultNumber(cves, 'count') <= 0) {
    return [];
  }
  const label = `${product || 'the product'}${version ? ` ${version}` : ''}`.trim();
  const entries = resultArray(cves, 'cves').slice(0, 5).map((entry) => {
    if (!isRecord(entry)) {
      return undefined;
    }
    const id = resultText(entry, 'id') || 'CVE';
    const severity = resultText(entry, 'severity') || 'unknown severity';
    return `${id} (${severity}): verify exposure for ${label} and patch or mitigate if applicable.`;
  }).filter((entry): entry is string => Boolean(entry));
  return entries.length ? entries : [`Review public CVEs for ${label} and confirm whether the deployed version is affected.`];
}

function authLogRecommendations(authLog: unknown): string[] {
  if (toolError(authLog)) {
    return [];
  }
  const attacking = resultArray(authLog, 'results').filter((entry) => isRecord(entry) && entry.status === 'attack');
  if (!attacking.length) {
    return [];
  }
  const ips = attacking.map((entry) => isRecord(entry) ? resultText(entry, 'ip') : undefined).filter(Boolean).slice(0, 8).join(', ');
  return [`Review and block confirmed brute-force sources: ${ips}. Rotate affected account credentials if any targeted usernames are valid.`];
}

function researchRecommendations(research: unknown): string[] {
  if (toolError(research) || resultNumber(research, 'count') <= 0) {
    return [];
  }
  return ['Use the cited research papers as supporting evidence for remediation priorities and incident notes.'];
}

function reportSummary(sections: ReportSection[]) {
  const warnings = sections.filter((section) => section.status === 'warning').length;
  const errors = sections.filter((section) => section.status === 'error').length;
  const skipped = sections.filter((section) => section.status === 'skipped').length;
  const status = errors ? 'attention_needed' : warnings ? 'review_needed' : 'ok';
  const headline = errors
    ? `${errors} section(s) failed and ${warnings} section(s) need review.`
    : warnings
      ? `${warnings} section(s) need review.`
      : 'No immediate high-risk findings in completed checks.';
  return {
    status,
    sections: sections.length,
    warnings,
    errors,
    skipped,
    headline,
  };
}

function buildReportMarkdown(report: SecurityReport): string {
  const lines = [
    `# ${report.title}`,
    '',
    `Report ID: ${report.report_id}`,
    `Created: ${report.created_at}`,
    '',
    `Summary: ${report.summary.headline}`,
    '',
    '## Sections',
  ];

  for (const section of report.sections) {
    lines.push('', `### ${section.name}`, `Status: ${section.status}`, section.summary);
  }

  lines.push('', '## Recommendations');
  for (const recommendation of report.recommendations) {
    lines.push(`- ${recommendation}`);
  }

  lines.push('', 'Email note: this report is not emailed unless send_email is explicitly true or send_report_email is called.');
  return lines.join('\n');
}

async function saveSecurityReport(appRoot: string, report: SecurityReport) {
  const dir = reportsDir(appRoot);
  await fs.mkdir(dir, { recursive: true });
  const file = path.join(dir, `${safeReportId(report.report_id)}.json`);
  const latest = path.join(dir, 'mcp-last-report.json');
  const data = JSON.stringify(report, null, 2);
  await fs.writeFile(file, data, 'utf8');
  await fs.writeFile(latest, data, 'utf8');
  return { latest, report: file };
}

async function loadSecurityReport(appRoot: string, reportId?: string): Promise<SecurityReport> {
  const dir = reportsDir(appRoot);
  const file = reportId
    ? path.join(dir, `${safeReportId(reportId).replace(/\.json$/i, '')}.json`)
    : path.join(dir, 'mcp-last-report.json');
  const raw = await fs.readFile(file, 'utf8');
  return JSON.parse(raw) as SecurityReport;
}

function defaultReportTitle(target?: string, product?: string): string {
  if (target && product) {
    return `Talos Security Report: ${target} and ${product}`;
  }
  if (target) {
    return `Talos Website Security Report: ${target}`;
  }
  if (product) {
    return `Talos Product Security Report: ${product}`;
  }
  return 'Talos Security Report';
}

async function generateSecurityReport(appRoot: string, args: JsonObject) {
  const target = optionalTextArg(args, 'target');
  const product = optionalTextArg(args, 'product');
  const version = optionalTextArg(args, 'version');
  const includeResearch = optionalBooleanArg(args, 'include_research') ?? true;
  const analyzeLog = optionalBooleanArg(args, 'analyze_auth_log') ?? false;
  const sendEmailRequested = optionalBooleanArg(args, 'send_email') === true;
  const title = optionalTextArg(args, 'title') || defaultReportTitle(target, product);
  const sections: ReportSection[] = [];
  const recommendations: string[] = [];

  if (target) {
    const scan = await scanWebsite(target);
    const findings = resultArray(scan, 'findings');
    sections.push({
      name: 'Website Scan',
      status: sectionStatus(scan, findings.length > 0),
      summary: summarizeWebsiteScan(scan),
      data: scan,
    });
    recommendations.push(...websiteRecommendations(scan));
  } else {
    sections.push({
      name: 'Website Scan',
      status: 'skipped',
      summary: 'No website target was provided, so no website scan was run.',
    });
  }

  if (product) {
    const cves = await lookupCves(product, version);
    sections.push({
      name: 'CVE Lookup',
      status: sectionStatus(cves, resultNumber(cves, 'count') > 0),
      summary: summarizeCves(cves),
      data: cves,
    });
    recommendations.push(...cveRecommendations(cves, product, version));
  } else {
    sections.push({
      name: 'CVE Lookup',
      status: 'skipped',
      summary: 'No product was provided, so no CVE lookup was run.',
    });
  }

  const researchQuery = optionalTextArg(args, 'research_query')
    || (product ? `${product} ${version || ''} security vulnerabilities mitigation`.trim() : undefined)
    || (target ? `${target} website security headers hardening`.trim() : undefined);
  if (includeResearch && researchQuery) {
    const research = await searchResearch(appRoot, {
      query: researchQuery,
      source: 'all',
      year_from: 2020,
      open_access: false,
    });
    sections.push({
      name: 'Security Research',
      status: sectionStatus(research, resultArray(research, 'provider_errors').length > 0),
      summary: summarizeResearch(research),
      data: research,
    });
    recommendations.push(...researchRecommendations(research));
  } else {
    sections.push({
      name: 'Security Research',
      status: 'skipped',
      summary: includeResearch
        ? 'No research query, product, or target was provided, so research search was skipped.'
        : 'Research citations were not requested.',
    });
  }

  if (analyzeLog) {
    const authLog = await analyzeAuthLog(appRoot, optionalTextArg(args, 'auth_log_path'), optionalNumberArg(args, 'threshold'));
    const summary = isRecord(authLog) && isRecord(authLog.summary) ? authLog.summary : {};
    sections.push({
      name: 'Auth Log Analysis',
      status: sectionStatus(authLog, resultNumber(summary, 'attacks') > 0 || resultNumber(summary, 'suspicious') > 0),
      summary: summarizeAuthLog(authLog),
      data: authLog,
    });
    recommendations.push(...authLogRecommendations(authLog));
  } else {
    sections.push({
      name: 'Auth Log Analysis',
      status: 'skipped',
      summary: 'Auth log analysis was not requested.',
    });
  }

  if (!recommendations.length) {
    recommendations.push('No immediate high-priority action was found in the completed checks. Review skipped sections if broader coverage is needed.');
  }

  const createdAt = new Date().toISOString();
  const report: SecurityReport = {
    report_id: reportTimestampId(new Date(createdAt)),
    created_at: createdAt,
    title,
    target,
    product,
    version,
    summary: reportSummary(sections),
    sections,
    recommendations,
    markdown: '',
    fallback: 'node',
  };
  report.markdown = buildReportMarkdown(report);
  report.saved = await saveSecurityReport(appRoot, report);

  if (sendEmailRequested) {
    report.email = await sendEmailMessage(appRoot, report.markdown, title, optionalTextArg(args, 'to'));
  } else {
    report.email = {
      sent: false,
      skipped: true,
      reason: 'Report generation is report-only by default. Set send_email=true or call send_report_email only when the user explicitly asks to email, send, forward, or share it.',
    };
  }

  await saveSecurityReport(appRoot, report);
  return report;
}

async function sendReportEmail(appRoot: string, args: JsonObject) {
  try {
    const report = await loadSecurityReport(appRoot, optionalTextArg(args, 'report_id'));
    const subject = optionalTextArg(args, 'subject') || report.title || 'Talos security report';
    const note = optionalTextArg(args, 'message');
    const body = note ? `${note}\n\n${report.markdown}` : report.markdown;
    const email = await sendEmailMessage(appRoot, body, subject, optionalTextArg(args, 'to'));
    return {
      ...email,
      report_id: report.report_id,
      report_title: report.title,
      explicit_email_tool: true,
      fallback: 'node',
    };
  } catch (error) {
    return {
      sent: false,
      error: `Could not send report: ${error instanceof Error ? error.message : String(error)}`,
      fallback: 'node',
    };
  }
}

async function getLastSecurityReport(appRoot: string) {
  try {
    return await loadSecurityReport(appRoot);
  } catch (error) {
    return {
      error: `No generated MCP report found yet: ${error instanceof Error ? error.message : String(error)}`,
      hint: 'Call generate_security_report first.',
      fallback: 'node',
    };
  }
}

function selfTestSummary(tool: string, result: unknown): string {
  const error = toolError(result);
  if (error) {
    return error;
  }
  if (tool === 'scan_website') {
    return summarizeWebsiteScan(result);
  }
  if (tool === 'lookup_cves') {
    return summarizeCves(result);
  }
  if (tool === 'analyze_auth_log') {
    return summarizeAuthLog(result);
  }
  if (tool === 'search_research') {
    return summarizeResearch(result);
  }
  if (tool === 'generate_security_report') {
    return isRecord(result) && isRecord(result.summary) ? String(result.summary.headline || 'Report generated.') : 'Report generated.';
  }
  if (isRecord(result) && typeof result.sent === 'boolean') {
    return result.sent ? 'Email/alert sent.' : String(result.error || 'Email/alert not sent.');
  }
  if (isRecord(result) && typeof result.count === 'number') {
    return `${result.count} result(s).`;
  }
  if (isRecord(result) && typeof result.total === 'number') {
    return `${result.total} total item(s).`;
  }
  return 'Tool completed.';
}

function compactSelfTestDetail(result: unknown): unknown {
  if (!isRecord(result)) {
    return result;
  }
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(result)) {
    if (['headers', 'password', 'all', 'markdown', 'sections'].includes(key)) {
      continue;
    }
    out[key] = value;
  }
  return out;
}

async function writeSelfTestAuthLog(appRoot: string): Promise<string> {
  const dir = reportsDir(appRoot);
  await fs.mkdir(dir, { recursive: true });
  const file = path.join(dir, 'mcp-self-test-auth.log');
  const sample = [
    'Jul 17 10:00:01 host sshd[100]: Failed password for invalid user admin from 203.0.113.10 port 51422 ssh2',
    'Jul 17 10:00:02 host sshd[101]: Failed password for invalid user root from 203.0.113.10 port 51423 ssh2',
    'Jul 17 10:00:03 host sshd[102]: Failed password for invalid user test from 203.0.113.10 port 51424 ssh2',
    'Jul 17 10:00:04 host sshd[103]: Failed password for invalid user demo from 203.0.113.10 port 51425 ssh2',
    'Jul 17 10:00:05 host sshd[104]: Failed password for invalid user oracle from 198.51.100.20 port 51426 ssh2',
  ].join('\n');
  await fs.writeFile(file, sample, 'utf8');
  return file;
}

async function selfTestAllTools(appRoot: string, args: JsonObject) {
  const includeEmail = optionalBooleanArg(args, 'include_email') === true;
  const target = optionalTextArg(args, 'target') || 'example.com';
  const startedAt = new Date().toISOString();
  const results: SelfTestItem[] = [];
  const authLogPath = await writeSelfTestAuthLog(appRoot);

  async function run(tool: string, fn: () => Promise<unknown> | unknown, skipReason?: string) {
    const start = Date.now();
    if (skipReason) {
      results.push({
        tool,
        status: 'skipped',
        duration_ms: 0,
        summary: skipReason,
      });
      return;
    }
    try {
      const result = await fn();
      const error = toolError(result);
      results.push({
        tool,
        status: error ? 'fail' : 'pass',
        duration_ms: Date.now() - start,
        summary: selfTestSummary(tool, result),
        detail: error ? compactSelfTestDetail(result) : undefined,
      });
    } catch (error) {
      results.push({
        tool,
        status: 'fail',
        duration_ms: Date.now() - start,
        summary: error instanceof Error ? error.message : String(error),
      });
    }
  }

  await run('scan_website', () => scanWebsite(target));
  await run('lookup_cves', () => lookupCves('nginx', '1.18.0'));
  await run('analyze_auth_log', () => analyzeAuthLog(appRoot, authLogPath, 3));
  await run('search_research', () => searchResearch(appRoot, {
    query: 'brute force login detection',
    source: 'openalex',
    year_from: 2020,
    open_access: false,
  }));
  await run('find_research_papers', () => findResearchPapers(appRoot, {
    findings: ['Missing HSTS', 'Missing Content Security Policy', 'Missing X-Frame-Options'],
    source: 'openalex',
    year_from: 2010,
    max_results: 5,
  }));
  await run('analyze_link_safety', () => analyzeLinkSafety(appRoot, {
    url: 'https://example.com',
    expected_brand: 'Example',
    follow_redirects: true,
    check_providers: false,
    privacy_mode: true,
  }));
  await run('generate_blocklist', () => generateBlocklist(['203.0.113.10', '198.51.100.20'], 3));
  await run('send_alert', () => sendAlert(appRoot, 'Talos MCP self-test alert', 'Talos MCP self-test'), includeEmail ? undefined : 'Skipped by default; set include_email=true to send a real alert.');
  await run('send_email', () => sendEmail(appRoot, 'Talos MCP self-test email', 'Talos MCP self-test'), includeEmail ? undefined : 'Skipped by default; set include_email=true to send a real email.');
  await run('generate_security_report', () => generateSecurityReport(appRoot, {
    target,
    product: 'nginx',
    version: '1.18.0',
    include_research: false,
    analyze_auth_log: true,
    auth_log_path: authLogPath,
    threshold: 3,
    send_email: false,
    title: 'Talos MCP Self-Test Report',
  }));
  await run('send_report_email', () => sendReportEmail(appRoot, {
    subject: 'Talos MCP self-test report',
    message: 'This is a Talos MCP self-test report email.',
  }), includeEmail ? undefined : 'Skipped by default; set include_email=true to email the generated report.');
  await run('check_password_strength', () => checkPasswordStrength('CorrectHorseBatteryStaple!2026'));
  await run('generate_password', () => generatePassword(20, true));
  await run('hash_text', () => hashText('talos-self-test', 'sha256'));
  await run('decode_jwt', () => decodeJwt('eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJ0YWxvcy1zZWxmLXRlc3QifQ.'));
  await run('lookup_ip', () => lookupIp('8.8.8.8'));
  await run('get_defense_status', () => getDefenseStatus());
  await run('list_security_tools', () => listSecurityTools('dns'));
  await run('run_security_tool', () => runSecurityTool('dns_lookup', { domain: 'example.com' }));
  await run('search_resources', () => searchResources(appRoot, 'security', 3));

  const books = await listBooks(appRoot);
  const firstBook = books[0];
  await run('get_resource_page', () => getResourcePage(appRoot, String(firstBook.book_id), 1), firstBook ? undefined : 'Skipped because no resource books are bundled.');
  await run('list_resources', () => ({ books, fallback: 'node' }));
  await run('self_test_all_tools', () => ({ running: true, note: 'This self-test invocation is active and did not recurse.', fallback: 'node' }));

  const passed = results.filter((item) => item.status === 'pass').length;
  const failed = results.filter((item) => item.status === 'fail').length;
  const skipped = results.filter((item) => item.status === 'skipped').length;
  return {
    status: failed ? 'fail' : 'pass',
    started_at: startedAt,
    completed_at: new Date().toISOString(),
    totals: {
      total: results.length,
      passed,
      failed,
      skipped,
    },
    email_tests: includeEmail ? 'enabled' : 'skipped',
    note: includeEmail
      ? 'Email tests were enabled and may send real messages.'
      : 'Email and report-email tests were skipped. Set include_email=true only when you want real messages sent.',
    results,
    fallback: 'node',
  };
}

function getDefenseStatus() {
  return {
    enabled: false,
    blocked_ips: [],
    blocked_count: 0,
    events_total: 0,
    attacks_by_type: {},
    recent_events: [],
    thresholds: {},
    note: 'Hosted Node fallback has no in-app defense middleware state. The Python/FastAPI app reports live defense events.',
    fallback: 'node',
  };
}

function listSecurityTools(search?: string, category?: string) {
  const cat = (category || '').trim().toLowerCase();
  const query = (search || '').trim().toLowerCase();
  const pool = SECURITY_TOOL_SPECS.filter((entry) => !cat || entry.category.toLowerCase() === cat);
  if (!query) {
    const counts = new Map<string, number>();
    for (const entry of SECURITY_TOOL_SPECS) {
      counts.set(entry.category, (counts.get(entry.category) || 0) + 1);
    }
    return {
      total: SECURITY_TOOL_SPECS.length,
      count: pool.length,
      categories: Array.from(counts.entries()).map(([name, tools]) => ({ name, tools })),
      tools: cat ? pool : undefined,
      hint: 'Search again with a keyword, for example "decode base64", "dns", "headers", or "password".',
      fallback: 'node',
    };
  }
  const terms = tokens(query);
  const scored = pool.map((entry) => {
    const haystack = `${entry.name} ${entry.category} ${entry.description} ${entry.inputs.join(' ')}`.toLowerCase();
    const score = terms.reduce((sum, term) => sum + (haystack.includes(term) ? 1 : 0), 0);
    return score ? { score, entry } : undefined;
  }).filter((entry): entry is { score: number; entry: typeof SECURITY_TOOL_SPECS[number] } => Boolean(entry));
  scored.sort((a, b) => b.score - a.score || a.entry.name.localeCompare(b.entry.name));
  return {
    query,
    match_count: scored.length,
    showing: Math.min(12, scored.length),
    tools: scored.slice(0, 12).map(({ entry }) => entry),
    next: 'Pick one and call run_security_tool with its name and inputs.',
    fallback: 'node',
  };
}

async function runSecurityTool(name: string, args: JsonObject) {
  switch (name.trim()) {
    case 'base64_encode':
      return { encoded: Buffer.from(textArg(args, 'text'), 'utf8').toString('base64'), fallback: 'node' };
    case 'base64_decode':
      return { decoded: Buffer.from(textArg(args, 'text'), 'base64').toString('utf8'), fallback: 'node' };
    case 'url_encode':
      return { encoded: encodeURIComponent(textArg(args, 'text')), fallback: 'node' };
    case 'url_decode':
      return { decoded: decodeURIComponent(textArg(args, 'text')), fallback: 'node' };
    case 'hash_text':
      return hashText(textArg(args, 'text'), optionalTextArg(args, 'algo') || 'sha256');
    case 'hash_identifier':
      return identifyHash(textArg(args, 'hash'));
    case 'jwt_decode':
      return decodeJwt(textArg(args, 'token'));
    case 'password_strength':
      return checkPasswordStrength(textArg(args, 'password'));
    case 'generate_password':
      return generatePassword(optionalNumberArg(args, 'length') ?? 20, optionalBooleanArg(args, 'symbols') ?? true);
    case 'dns_lookup':
      return dnsLookup(textArg(args, 'domain'));
    case 'lookup_ip':
      return lookupIp(textArg(args, 'ip'));
    case 'lookup_cves':
      return lookupCves(textArg(args, 'product'), optionalTextArg(args, 'version'));
    case 'http_headers':
      return httpHeaders(textArg(args, 'url'));
    case 'security_headers':
      return scanWebsite(textArg(args, 'url'));
    case 'robots_txt':
      return robotsTxt(textArg(args, 'url'));
    default:
      return {
        error: `Unknown Node fallback tool: ${name}`,
        note: 'Use list_security_tools to see tools available without Python. The full catalog requires the Python runtime.',
        fallback: 'node',
      };
  }
}

function identifyHash(hash: string) {
  const clean = hash.trim();
  const hex = /^[a-f0-9]+$/i.test(clean);
  const candidates = [];
  if (hex && clean.length === 32) {
    candidates.push('MD5');
  }
  if (hex && clean.length === 40) {
    candidates.push('SHA-1');
  }
  if (hex && clean.length === 64) {
    candidates.push('SHA-256');
  }
  if (hex && clean.length === 128) {
    candidates.push('SHA-512');
  }
  return { hash: clean, candidates, note: candidates.length ? 'Length-based guess only.' : 'No common hash match.', fallback: 'node' };
}

async function dnsLookup(domain: string) {
  const target = cleanTarget(domain);
  if (!target) {
    return { error: 'Domain is required.', fallback: 'node' };
  }
  const out: Record<string, unknown> = { domain: target, fallback: 'node' };
  const lookups: Array<[string, () => Promise<unknown>]> = [
    ['A', () => dns.resolve4(target)],
    ['AAAA', () => dns.resolve6(target)],
    ['MX', () => dns.resolveMx(target)],
    ['TXT', () => dns.resolveTxt(target)],
    ['NS', () => dns.resolveNs(target)],
  ];
  for (const [key, fn] of lookups) {
    try {
      out[key] = await fn();
    } catch {
      out[key] = [];
    }
  }
  return out;
}

async function httpHeaders(rawUrl: string) {
  const url = normalizeWebsiteUrl(rawUrl);
  if (!url) {
    return { error: 'Provide a valid URL.', fallback: 'node' };
  }
  try {
    const response = await fetchWithTimeout(url.toString(), { method: 'GET', redirect: 'follow' }, 12000);
    return {
      url: url.toString(),
      status_code: response.status,
      headers: Object.fromEntries(response.headers.entries()),
      fallback: 'node',
    };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error), url: url.toString(), fallback: 'node' };
  }
}

async function robotsTxt(rawUrl: string) {
  const url = normalizeWebsiteUrl(rawUrl);
  if (!url) {
    return { error: 'Provide a valid URL.', fallback: 'node' };
  }
  url.pathname = '/robots.txt';
  url.search = '';
  try {
    const response = await fetchWithTimeout(url.toString(), { method: 'GET', redirect: 'follow' }, 12000);
    const text = await response.text();
    return {
      url: url.toString(),
      status_code: response.status,
      text: text.slice(0, 6000),
      truncated: text.length > 6000,
      fallback: 'node',
    };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error), url: url.toString(), fallback: 'node' };
  }
}

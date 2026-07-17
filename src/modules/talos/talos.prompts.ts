import { PromptDecorator as Prompt, ExecutionContext } from '@nitrostack/core';

export class TalosPrompts {
  @Prompt({
    name: 'talos_website_security_review',
    title: 'Talos Website Security Review',
    description: 'Guide an assistant to perform a safe website security review with scan_website and optional CVE lookup.',
    arguments: [
      {
        name: 'target',
        description: 'Website or URL to review.',
        required: true,
      },
    ],
  })
  async websiteSecurityReview(args: { target?: string }, ctx: ExecutionContext) {
    ctx.logger.info('Creating website security review prompt', { target: args.target });

    return [{
      role: 'user' as const,
      content: `Run a defensive Talos review for ${args.target || 'the target website'}.

Use scan_website first. Summarize the grade, the highest-risk findings, and the concrete fixes. If the scan reveals a named product and version, use lookup_cves before giving final remediation. Keep the tone clear and practical, and remind the user that scans should only be run on systems they own or are authorized to test.`,
    }];
  }

  @Prompt({
    name: 'talos_full_security_report',
    title: 'Talos Full Security Report',
    description: 'Guide an assistant to generate a consolidated Talos report without emailing unless explicitly requested.',
    arguments: [
      {
        name: 'target',
        description: 'Optional website or URL to include in the report.',
        required: false,
      },
      {
        name: 'product',
        description: 'Optional product/software name for CVE lookup.',
        required: false,
      },
      {
        name: 'version',
        description: 'Optional product version for CVE lookup.',
        required: false,
      },
    ],
  })
  async fullSecurityReport(args: { target?: string; product?: string; version?: string }, ctx: ExecutionContext) {
    ctx.logger.info('Creating full security report prompt', args);

    const targetLine = args.target ? `target: ${args.target}` : 'target: not provided';
    const productLine = args.product ? `product: ${args.product}${args.version ? ` ${args.version}` : ''}` : 'product: not provided';

    return [{
      role: 'user' as const,
      content: `Generate a Talos security report with generate_security_report.

Inputs:
- ${targetLine}
- ${productLine}

Return the report and key recommendations to the user. If the user asks for research papers, citations, studies, references, or academic literature about the findings, call find_research_papers. Do not call send_email, send_alert, or send_report_email unless the user explicitly asks to email, send, forward, or share the report. If the user only asks for website scan results or the report contents, only return the report contents.`,
    }];
  }

  @Prompt({
    name: 'talos_research_paper_lookup',
    title: 'Talos Research Paper Lookup',
    description: 'Guide an assistant to call find_research_papers for papers/citations about scan findings or security topics.',
    arguments: [
      {
        name: 'topic',
        description: 'Security topic or finding to search papers for.',
        required: false,
      },
    ],
  })
  async researchPaperLookup(args: { topic?: string }, ctx: ExecutionContext) {
    ctx.logger.info('Creating research paper lookup prompt', args);

    return [{
      role: 'user' as const,
      content: `Find academic research papers and citations with find_research_papers${args.topic ? ` for ${args.topic}` : ''}. Use real returned papers from the tool; do not say the MCP server lacks literature search unless find_research_papers and search_research are both unavailable or return errors.`,
    }];
  }

  @Prompt({
    name: 'talos_link_safety_check',
    title: 'Talos Link Safety Check',
    description: 'Guide an assistant to call analyze_link_safety for phishing, scam, malware, redirect, impersonation, or safe-link questions.',
    arguments: [
      {
        name: 'url',
        description: 'URL/link to check.',
        required: true,
      },
      {
        name: 'expected_brand',
        description: 'Optional claimed brand or service.',
        required: false,
      },
    ],
  })
  async linkSafetyCheck(args: { url?: string; expected_brand?: string }, ctx: ExecutionContext) {
    ctx.logger.info('Creating link safety check prompt', { url: args.url, expected_brand: args.expected_brand });

    const brand = args.expected_brand ? ` with expected_brand=${args.expected_brand}` : '';
    return [{
      role: 'user' as const,
      content: `Check whether ${args.url || 'the provided link'} is safe using analyze_link_safety${brand}. Explain the verdict, risk score, strongest reasons, safe points, and whether the user should enter credentials/payment information. Do not claim a link is safe without using the tool when analyze_link_safety is available.`,
    }];
  }

  @Prompt({
    name: 'talos_bruteforce_incident',
    title: 'Talos Brute-Force Incident Review',
    description: 'Guide an assistant through auth log analysis and defensive mitigation generation.',
    arguments: [
      {
        name: 'path',
        description: 'Optional authentication log path.',
        required: false,
      },
      {
        name: 'threshold',
        description: 'Optional failed-attempt threshold.',
        required: false,
      },
    ],
  })
  async bruteforceIncident(args: { path?: string; threshold?: string }, ctx: ExecutionContext) {
    ctx.logger.info('Creating brute-force incident prompt', args);

    const pathPart = args.path ? ` at ${args.path}` : '';
    const thresholdPart = args.threshold ? ` using threshold ${args.threshold}` : '';

    return [{
      role: 'user' as const,
      content: `Analyze the authentication log${pathPart}${thresholdPart} with analyze_auth_log. Identify attacking IPs, targeted accounts, confidence/anomaly signals, and likely severity. If attackers are confirmed, call generate_blocklist for the attacking IPs, but do not claim the rules have been applied. End with a short incident-response checklist.`,
    }];
  }

  @Prompt({
    name: 'talos_tool_health_check',
    title: 'Talos Tool Health Check',
    description: 'Guide an assistant to run the Nitro-compatible Talos tool self-test without sending email by default.',
    arguments: [
      {
        name: 'target',
        description: 'Optional safe website target for scan tests.',
        required: false,
      },
    ],
  })
  async toolHealthCheck(args: { target?: string }, ctx: ExecutionContext) {
    ctx.logger.info('Creating Talos tool health-check prompt', args);

    return [{
      role: 'user' as const,
      content: `Run self_test_all_tools with include_email=false${args.target ? ` and target=${args.target}` : ''}. Summarize pass/fail/skipped counts and list any failed tools with the returned error. Do not send test email unless the user explicitly requests include_email=true.`,
    }];
  }
}

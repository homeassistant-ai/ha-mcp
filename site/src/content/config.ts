import { defineCollection, z } from 'astro:content';

const clients = defineCollection({
  type: 'content',
  schema: z.object({
    name: z.string(),
    company: z.string(),
    logo: z.string(),
    transports: z.array(z.enum(['stdio', 'sse', 'streamable-http'])),
    configFormat: z.enum(['json', 'toml', 'yaml', 'cli', 'ui']),
    configLocation: z.string().optional(),
    accuracy: z.number().min(1).max(5),
    order: z.number().default(99),
    httpNote: z.string().optional(), // e.g., "requires mcp-proxy"
    beta: z.boolean().optional(), // mark as BETA if uncertain
  })
});

const platforms = defineCollection({
  type: 'content',
  schema: z.object({
    name: z.string(),
    icon: z.string(),
    order: z.number(),
  })
});

const connections = defineCollection({
  type: 'content',
  schema: z.object({
    name: z.string(),
    transport: z.enum(['stdio', 'http', 'https']),
    description: z.string(),
    icon: z.string(),
    order: z.number(),
  })
});

const deployment = defineCollection({
  type: 'content',
  schema: z.object({
    name: z.string(),
    description: z.string(),
    icon: z.string(),
    forConnections: z.array(z.enum(['local', 'network', 'remote'])),
    order: z.number(),
  })
});

export const collections = {
  clients,
  platforms,
  connections,
  deployment,
};

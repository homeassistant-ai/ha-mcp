import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

const clients = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/clients' }),
  schema: z.object({
    name: z.string(),
    company: z.string(),
    logo: z.string(),
    transports: z.array(z.enum(['stdio', 'sse', 'streamable-http'])),
    configFormat: z.enum(['json', 'toml', 'yaml', 'cli', 'ui']),
    configLocation: z.string().optional(),
    accuracy: z.number().min(1).max(5),
    order: z.number().default(99),
    httpNote: z.string().optional(),
    beta: z.boolean().optional(),
  }),
});

const platforms = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/platforms' }),
  schema: z.object({
    name: z.string(),
    icon: z.string(),
    order: z.number(),
  }),
});

const connections = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/connections' }),
  schema: z.object({
    name: z.string(),
    transport: z.enum(['stdio', 'http', 'https']),
    description: z.string(),
    icon: z.string(),
    order: z.number(),
  }),
});

const deployment = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/deployment' }),
  schema: z.object({
    name: z.string(),
    description: z.string(),
    icon: z.string(),
    forConnections: z.array(z.enum(['local', 'network', 'remote'])),
    order: z.number(),
  }),
});

export const collections = {
  clients,
  platforms,
  connections,
  deployment,
};

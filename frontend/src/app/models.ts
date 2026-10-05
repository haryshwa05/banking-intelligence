export const API_URL = 'http://localhost:8000';

export type KnowledgeKind = 'entity' | 'reference' | 'operational';
export type CustomerAccess = 'required' | 'optional' | 'none';

export const KIND_LABELS: Record<KnowledgeKind, string> = {
  entity: 'Entity knowledge',
  reference: 'Reference knowledge',
  operational: 'Operational knowledge',
};

export const KIND_DESCRIPTIONS: Record<KnowledgeKind, string> = {
  entity: 'Customer-specific documents. Private to one customer and only used in that customer\'s chats.',
  reference: 'Shared policies, regulations, product rules and eligibility criteria.',
  operational: 'Shared SOPs, checklists, review procedures and escalation guides.',
};

export const CUSTOMER_ACCESS_LABELS: Record<CustomerAccess, string> = {
  required: 'Works on one customer (required)',
  optional: 'Customer optional',
  none: 'No customer documents',
};

export interface KnowledgeSpace {
  id: string;
  kind: Exclude<KnowledgeKind, 'entity'>;
  kindLabel: string;
  name: string;
  description: string;
  documentCount: number;
}

export interface Customer { id: string; name: string; documentCount: number; }

export interface Agent {
  id: string;
  name: string;
  purpose: string;
  description: string;
  instructions: string;
  model: string;
  customerAccess: CustomerAccess;
  spaceIds: string[];
  capabilities: string[];
  builtIn: boolean;
  conversationCount: number;
}

export interface CatalogItem { id: string; label: string; description: string; }
export interface AgentCatalog { models: CatalogItem[]; capabilities: CatalogItem[]; defaultModel: string; }

export interface KnowledgeSource { id: string; kind: KnowledgeKind; name: string; documentCount: number; }
export interface KnowledgeInUse {
  agent: { id: string; name: string };
  entity: { id: string; name: string } | null;
  sources: KnowledgeSource[];
  documentCount: number;
}

/** A stable, light colour tone per agent/customer so each is recognisable at a glance. */
export function toneFor(id: string | null | undefined): string {
  let hash = 0;
  for (const character of id || '') hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return `tone-${(hash % 6) + 1}`;
}

export function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map(word => word[0].toUpperCase()).join('');
}

export const KIND_ICONS: Record<KnowledgeKind, string> = { entity: 'user', reference: 'book', operational: 'checklist' };

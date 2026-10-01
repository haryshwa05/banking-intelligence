import { IconComponent } from './icon.component';
import { CommonModule } from '@angular/common';
import { HttpClient, HttpClientModule } from '@angular/common/http';
import { Component, EventEmitter, Input, OnChanges, OnInit, Output } from '@angular/core';
import { API_URL, Agent, AgentCatalog, CUSTOMER_ACCESS_LABELS, CustomerAccess, KIND_LABELS, KnowledgeSpace } from './models';

interface AgentDraft {
  name: string; purpose: string; description: string; instructions: string; model: string;
  customerAccess: CustomerAccess; spaceIds: string[]; capabilities: string[];
}

@Component({
  selector: 'app-agent-builder',
  standalone: true,
  imports: [IconComponent, CommonModule, HttpClientModule],
  template: `
    <section *ngIf="!editAgentId" class="workspace builder-workspace">
      <div class="workspace-toolbar">
        <div><h1>Agent Builder</h1><span class="document-count">Define what each agent is for, what knowledge it may read, and what it is allowed to do.</span></div>
        <button type="button" class="upload-button" (click)="open('new')"><app-icon name="plus"></app-icon>Create agent</button>
      </div>
      <p *ngIf="error" class="error-banner">{{ error }}</p>
      <section class="document-table builder-table">
        <div class="table-heading builder-row"><span>Agent</span><span>Customer documents</span><span>Shared knowledge</span><span>Capabilities</span><span></span></div>
        <div *ngFor="let agent of agents" class="builder-row">
          <span class="builder-name"><strong>{{ agent.name }}</strong><small>{{ agent.purpose }}</small><em *ngIf="agent.builtIn">Built-in</em></span>
          <span>{{ accessLabels[agent.customerAccess] }}</span>
          <span>{{ spaceNames(agent.spaceIds) || 'None' }}</span>
          <span>{{ agent.capabilities.length }} of {{ catalog?.capabilities?.length || 0 }}</span>
          <span class="builder-actions"><button type="button" class="link-button" (click)="chatStarted.emit(agent.id)">Chat</button><button type="button" class="link-button" (click)="open(agent.id)">Edit</button><button *ngIf="!agent.builtIn" type="button" class="link-button danger" (click)="remove(agent)">Delete</button></span>
        </div>
      </section>
    </section>

    <section *ngIf="editAgentId && draft" class="workspace builder-workspace">
      <div class="workspace-toolbar">
        <div><h1>{{ editAgentId === 'new' ? 'Create agent' : 'Edit ' + (original?.name || 'agent') }}</h1><span class="document-count">Changes to knowledge access apply to the agent's existing chats from their next question.</span></div>
      </div>
      <p *ngIf="error" class="error-banner">{{ error }}</p>
      <div class="builder-layout">
        <form class="builder-form" (submit)="$event.preventDefault(); save()">
          <fieldset>
            <legend>1 · Role</legend>
            <label>Agent name<input [value]="draft.name" (input)="draft.name = $any($event.target).value" maxlength="80" placeholder="e.g. Home Loan Analyst"></label>
            <label>Purpose <small>One sentence that tells users when to use this agent.</small><input [value]="draft.purpose" (input)="draft.purpose = $any($event.target).value" maxlength="160" placeholder="e.g. Checks home loan applications against home loan policy."></label>
            <label>Description <small>Optional. Shown when someone starts a chat.</small><textarea rows="2" [value]="draft.description" (input)="draft.description = $any($event.target).value" maxlength="1000"></textarea></label>
          </fieldset>

          <fieldset>
            <legend>2 · Knowledge access</legend>
            <p class="fieldset-help">An agent can read only what you tick here. One customer is chosen per chat, and other customers' documents are never available.</p>
            <div class="choice-group">
              <label *ngFor="let option of accessOptions" class="choice" [class.selected]="draft.customerAccess === option.id">
                <input type="radio" name="customerAccess" [checked]="draft.customerAccess === option.id" (change)="draft.customerAccess = option.id">
                <span><strong>{{ option.label }}</strong><small>{{ option.help }}</small></span>
              </label>
            </div>
            <div *ngFor="let kind of sharedKinds" class="space-choices">
              <h3>{{ kindLabels[kind] }}</h3>
              <label *ngFor="let space of spacesOf(kind)" class="choice" [class.selected]="draft.spaceIds.includes(space.id)">
                <input type="checkbox" [checked]="draft.spaceIds.includes(space.id)" (change)="toggle(draft.spaceIds, space.id, $any($event.target).checked)">
                <span><strong>{{ space.name }} <em>{{ space.documentCount }} doc{{ space.documentCount === 1 ? '' : 's' }}</em></strong><small>{{ space.description }}</small></span>
              </label>
              <p *ngIf="!spacesOf(kind).length" class="fieldset-help">No {{ kindLabels[kind].toLowerCase() }} spaces exist yet. Create one in Knowledge Spaces.</p>
            </div>
          </fieldset>

          <fieldset>
            <legend>3 · Capabilities</legend>
            <p class="fieldset-help">What the agent is allowed to do with that knowledge.</p>
            <label *ngFor="let capability of catalog?.capabilities" class="choice" [class.selected]="draft.capabilities.includes(capability.id)">
              <input type="checkbox" [checked]="draft.capabilities.includes(capability.id)" (change)="toggle(draft.capabilities, capability.id, $any($event.target).checked)">
              <span><strong>{{ capability.label }}</strong><small>{{ capability.description }}</small></span>
            </label>
          </fieldset>

          <fieldset>
            <legend>4 · Instructions</legend>
            <label>How should this agent work? <small>Role, focus and tone. Answers always stay grounded in cited evidence, whatever the instructions say.</small>
              <textarea rows="6" [value]="draft.instructions" (input)="draft.instructions = $any($event.target).value" maxlength="4000" placeholder="e.g. Act as a home loan analyst. Relate each customer value to the policy condition it is tested against…"></textarea></label>
          </fieldset>

          <fieldset>
            <legend>5 · Model</legend>
            <label>Model
              <select [value]="draft.model" (change)="draft.model = $any($event.target).value">
                <option *ngFor="let model of catalog?.models" [value]="model.id">{{ model.label }}: {{ model.description }}</option>
              </select>
              <small>Only approved models are listed. Additional models need administrator approval.</small>
            </label>
          </fieldset>

          <div class="form-actions"><button type="button" class="link-button" (click)="closed.emit()">Cancel</button><button type="submit" class="primary-button" [disabled]="isSaving || !canSave()">{{ isSaving ? 'Saving…' : editAgentId === 'new' ? 'Create agent' : 'Save changes' }}</button></div>
        </form>

        <aside class="builder-summary">
          <h2>What this agent can access</h2>
          <div class="summary-block"><small>When a chat starts</small>
            <p *ngIf="draft.customerAccess === 'required'">The user must choose one customer. Only that customer's documents are used.</p>
            <p *ngIf="draft.customerAccess === 'optional'">The user may choose one customer, or chat about shared knowledge only.</p>
            <p *ngIf="draft.customerAccess === 'none'">No customer documents are ever used.</p>
          </div>
          <div class="summary-block"><small>Knowledge in use</small>
            <div class="knowledge-chips">
              <span *ngIf="draft.customerAccess !== 'none'" class="knowledge-chip kind-entity">Selected customer's documents</span>
              <span *ngFor="let space of selectedSpaces()" [class]="'knowledge-chip kind-' + space.kind">{{ space.name }}</span>
              <span *ngIf="draft.customerAccess === 'none' && !selectedSpaces().length" class="knowledge-empty">Nothing yet. Grant at least one source.</span>
            </div>
          </div>
          <div class="summary-block"><small>Can</small><ul><li *ngFor="let capability of enabledCapabilities()">{{ capability.label }}</li><li *ngIf="!enabledCapabilities().length" class="knowledge-empty">Nothing yet. Choose a capability.</li></ul></div>
          <div class="summary-block muted"><small>Cannot</small><ul><li *ngFor="let capability of disabledCapabilities()">{{ capability.label }}</li><li>Read other customers' documents</li><li>Read spaces not ticked above</li></ul></div>
        </aside>
      </div>
    </section>
  `
})
export class AgentBuilderComponent implements OnInit, OnChanges {
  @Input() editAgentId: string | null = null;
  @Output() agentsChanged = new EventEmitter<void>();
  @Output() chatStarted = new EventEmitter<string>();
  @Output() closed = new EventEmitter<void>();

  readonly accessLabels = CUSTOMER_ACCESS_LABELS;
  readonly kindLabels = KIND_LABELS;
  readonly sharedKinds: Array<'reference' | 'operational'> = ['reference', 'operational'];
  readonly accessOptions: Array<{ id: CustomerAccess; label: string; help: string }> = [
    { id: 'required', label: 'Works on one customer', help: 'Every chat is about one selected customer, e.g. eligibility or document review.' },
    { id: 'optional', label: 'Customer optional', help: 'Chats can be about one customer or about shared knowledge only, e.g. policy questions.' },
    { id: 'none', label: 'Shared knowledge only', help: 'Never reads customer documents, e.g. a policy or procedures assistant.' },
  ];
  agents: Agent[] = [];
  spaces: KnowledgeSpace[] = [];
  catalog: AgentCatalog | null = null;
  original: Agent | null = null;
  draft: AgentDraft | null = null;
  isSaving = false;
  error = '';

  constructor(private readonly http: HttpClient) {}
  ngOnInit(): void {
    this.http.get<AgentCatalog>(`${API_URL}/agents/catalog`).subscribe({ next: catalog => { this.catalog = catalog; this.prepareDraft(); } });
    this.http.get<KnowledgeSpace[]>(`${API_URL}/knowledge/spaces`).subscribe({ next: spaces => this.spaces = spaces });
    this.loadAgents();
  }
  ngOnChanges(): void { this.error = ''; this.prepareDraft(); }

  private loadAgents(): void {
    this.http.get<Agent[]>(`${API_URL}/agents`).subscribe({ next: agents => { this.agents = agents; this.prepareDraft(); }, error: () => this.error = 'Unable to load agents.' });
  }
  private prepareDraft(): void {
    if (!this.editAgentId) { this.draft = null; this.original = null; return; }
    if (this.editAgentId === 'new') {
      if (this.draft && !this.original) return;
      this.original = null;
      this.draft = { name: '', purpose: '', description: '', instructions: '', model: this.catalog?.defaultModel || 'claude-haiku-4-5-20251001',
        customerAccess: 'required', spaceIds: [], capabilities: ['document_search', 'fact_lookup'] };
      return;
    }
    const agent = this.agents.find(item => item.id === this.editAgentId);
    if (!agent || this.original?.id === agent.id) return;
    this.original = agent;
    this.draft = { name: agent.name, purpose: agent.purpose, description: agent.description, instructions: agent.instructions, model: agent.model,
      customerAccess: agent.customerAccess, spaceIds: [...agent.spaceIds], capabilities: [...agent.capabilities] };
  }

  open(id: string): void { window.location.hash = `#/builder/${encodeURIComponent(id)}`; }
  spacesOf(kind: string): KnowledgeSpace[] { return this.spaces.filter(space => space.kind === kind); }
  spaceNames(ids: string[]): string { return this.spaces.filter(space => ids.includes(space.id)).map(space => space.name).join(', '); }
  selectedSpaces(): KnowledgeSpace[] { return this.draft ? this.spaces.filter(space => this.draft!.spaceIds.includes(space.id)) : []; }
  enabledCapabilities() { return (this.catalog?.capabilities || []).filter(item => this.draft?.capabilities.includes(item.id)); }
  disabledCapabilities() { return (this.catalog?.capabilities || []).filter(item => !this.draft?.capabilities.includes(item.id)); }
  toggle(list: string[], id: string, checked: boolean): void {
    const index = list.indexOf(id);
    if (checked && index < 0) list.push(id);
    if (!checked && index >= 0) list.splice(index, 1);
  }
  canSave(): boolean {
    const draft = this.draft;
    return !!draft && draft.name.trim().length >= 2 && draft.capabilities.length > 0 && (draft.customerAccess !== 'none' || draft.spaceIds.length > 0);
  }
  save(): void {
    if (!this.draft || !this.canSave()) return;
    this.isSaving = true;
    this.error = '';
    const request = this.editAgentId === 'new'
      ? this.http.post<Agent>(`${API_URL}/agents`, this.draft)
      : this.http.put<Agent>(`${API_URL}/agents/${encodeURIComponent(this.editAgentId!)}`, this.draft);
    request.subscribe({
      next: () => { this.isSaving = false; this.draft = null; this.original = null; this.agentsChanged.emit(); this.loadAgents(); this.closed.emit(); },
      error: response => { this.isSaving = false; this.error = response.error?.detail || 'Could not save the agent.'; }
    });
  }
  remove(agent: Agent): void {
    const chats = agent.conversationCount ? ` Its ${agent.conversationCount} chat${agent.conversationCount === 1 ? '' : 's'} will also be deleted.` : '';
    if (!window.confirm(`Delete the agent "${agent.name}"?${chats} This cannot be undone.`)) return;
    this.http.delete(`${API_URL}/agents/${encodeURIComponent(agent.id)}`).subscribe({
      next: () => { this.agentsChanged.emit(); this.loadAgents(); },
      error: response => this.error = response.error?.detail || 'Could not delete the agent.'
    });
  }
}

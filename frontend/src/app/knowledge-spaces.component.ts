import { IconComponent } from './icon.component';
import { CommonModule } from '@angular/common';
import { HttpClient, HttpClientModule } from '@angular/common/http';
import { Component, EventEmitter, Input, OnInit, Output } from '@angular/core';
import { API_URL, Agent, Customer, KIND_DESCRIPTIONS, KIND_LABELS, KnowledgeKind, KnowledgeSpace } from './models';

interface SpaceDocument {
  id: string; name: string; type: string; uploadedAt: string;
  extractionStatus: string; indexStatus?: string | null; factStatus?: string | null;
  entityId?: string | null; entityName?: string | null; entityStatus?: string | null;
  knowledgeKind: KnowledgeKind; spaceId?: string | null; spaceName?: string | null;
}

@Component({
  selector: 'app-knowledge-spaces',
  standalone: true,
  imports: [IconComponent, CommonModule, HttpClientModule],
  template: `
    <section class="workspace knowledge-workspace">
      <div class="workspace-toolbar">
        <div><h1>Knowledge Spaces</h1><span class="document-count">Every document belongs to one knowledge context. Agents can only read the spaces they are granted.</span></div>
        <button type="button" class="upload-button" (click)="startCreate()"><app-icon name="plus"></app-icon>New shared space</button>
      </div>
      <p *ngIf="error" class="error-banner">{{ error }}</p>

      <form *ngIf="editing" class="space-editor" (submit)="$event.preventDefault(); saveSpace()">
        <h2>{{ editing.id ? 'Edit space' : 'New shared space' }}</h2>
        <label>Name<input [value]="editing.name" (input)="editing.name = $any($event.target).value" maxlength="80" placeholder="e.g. Home Loan Policies"></label>
        <label *ngIf="!editing.id">Kind
          <select [value]="editing.kind" (change)="editing.kind = $any($event.target).value">
            <option value="reference">Reference knowledge: policies, regulations, product rules</option>
            <option value="operational">Operational knowledge: SOPs, checklists, escalation guides</option>
          </select>
        </label>
        <label>Description<textarea rows="2" [value]="editing.description" (input)="editing.description = $any($event.target).value" maxlength="400" placeholder="What belongs in this space?"></textarea></label>
        <div class="form-actions"><button type="button" class="link-button" (click)="editing = null">Cancel</button><button type="submit" class="primary-button" [disabled]="editing.name.trim().length < 2">Save space</button></div>
      </form>

      <section class="knowledge-kind kind-entity">
        <header><span class="kind-badge kind-entity">{{ kindLabels.entity }}</span><p>{{ kindDescriptions.entity }}</p></header>
        <div *ngIf="isLoading" class="empty-table">Loading knowledge…</div>
        <div *ngIf="!isLoading && !customers.length && !unassigned().length" class="empty-table">No customer documents yet. Upload documents in the Document Repository; customers are detected automatically.</div>
        <article *ngFor="let customer of customers" class="space-card">
          <button type="button" class="space-card-header" (click)="toggle('entity:' + customer.id)">
            <span class="space-icon kind-entity">{{ initials(customer.name) }}</span>
            <span class="space-title"><strong>{{ customer.name }}</strong><small>Customer · private to this customer's chats</small></span>
            <span class="space-count">{{ entityDocuments(customer.id).length }} doc{{ entityDocuments(customer.id).length === 1 ? '' : 's' }}</span>
            <span class="space-caret"><app-icon [name]="expanded.has('entity:' + customer.id) ? 'down' : 'chevron'"></app-icon></span>
          </button>
          <div *ngIf="expanded.has('entity:' + customer.id)" class="space-body">
            <ng-container *ngTemplateOutlet="documentList; context: { $implicit: entityDocuments(customer.id) }"></ng-container>
            <div class="space-actions">
              <span>Work on {{ customer.name }} with</span>
              <button *ngFor="let agent of customerAgents()" type="button" class="link-button" (click)="chatStarted.emit({ agentId: agent.id, entityId: customer.id })">{{ agent.name }}</button>
            </div>
          </div>
        </article>
        <article *ngIf="unassigned().length" class="space-card review">
          <button type="button" class="space-card-header" (click)="toggle('entity:unassigned')">
            <span class="space-icon review"><app-icon name="info"></app-icon></span>
            <span class="space-title"><strong>Unassigned customer documents</strong><small>Not used by any agent until a customer is assigned or the document is filed into a shared space</small></span>
            <span class="space-count">{{ unassigned().length }} doc{{ unassigned().length === 1 ? '' : 's' }}</span>
            <span class="space-caret"><app-icon [name]="expanded.has('entity:unassigned') ? 'down' : 'chevron'"></app-icon></span>
          </button>
          <div *ngIf="expanded.has('entity:unassigned')" class="space-body"><ng-container *ngTemplateOutlet="documentList; context: { $implicit: unassigned() }"></ng-container></div>
        </article>
      </section>

      <section *ngFor="let kind of sharedKinds" class="knowledge-kind" [class]="'knowledge-kind kind-' + kind">
        <header><span [class]="'kind-badge kind-' + kind">{{ kindLabels[kind] }}</span><p>{{ kindDescriptions[kind] }}</p></header>
        <div *ngIf="!isLoading && !spacesOf(kind).length" class="empty-table">No {{ kindLabels[kind].toLowerCase() }} spaces yet.</div>
        <article *ngFor="let space of spacesOf(kind)" class="space-card">
          <button type="button" class="space-card-header" (click)="toggle(space.id)">
            <span [class]="'space-icon kind-' + kind"><app-icon [name]="kind === 'reference' ? 'book' : 'checklist'"></app-icon></span>
            <span class="space-title"><strong>{{ space.name }}</strong><small>{{ space.description || 'Shared with every customer context' }}</small></span>
            <span class="space-count">{{ spaceDocuments(space.id).length }} doc{{ spaceDocuments(space.id).length === 1 ? '' : 's' }}</span>
            <span class="space-caret"><app-icon [name]="expanded.has(space.id) ? 'down' : 'chevron'"></app-icon></span>
          </button>
          <div *ngIf="expanded.has(space.id)" class="space-body">
            <div class="space-used-by"><small>Used by</small>
              <span *ngFor="let agent of agentsUsing(space.id)" class="knowledge-chip">{{ agent.name }}</span>
              <span *ngIf="!agentsUsing(space.id).length" class="knowledge-empty">No agent has access yet. Grant it in the Agent Builder.</span>
            </div>
            <ng-container *ngTemplateOutlet="documentList; context: { $implicit: spaceDocuments(space.id) }"></ng-container>
            <div class="space-actions">
              <label class="link-button" [attr.for]="'space-upload-' + space.id">{{ uploadingSpaceId === space.id ? 'Uploading…' : 'Add documents' }}</label>
              <input class="hidden-file-input" type="file" multiple accept="image/*,.pdf" [id]="'space-upload-' + space.id" (change)="uploadInto(space, $event)" [disabled]="!!uploadingSpaceId">
              <button type="button" class="link-button" (click)="startEdit(space)">Edit</button>
              <button type="button" class="link-button danger" (click)="deleteSpace(space)" [disabled]="spaceDocuments(space.id).length > 0" [title]="spaceDocuments(space.id).length ? 'Move or delete its documents first' : 'Delete space'">Delete</button>
            </div>
          </div>
        </article>
      </section>
    </section>

    <ng-template #documentList let-documents>
      <div *ngIf="!documents.length" class="space-empty">No documents in this space yet.</div>
      <button *ngFor="let document of documents" type="button" class="space-document" (click)="documentOpened.emit(document.id)">
        <span class="file-icon" [class.image]="document.type.startsWith('image/')"><app-icon [name]="document.type.startsWith('image/') ? 'image' : 'file'"></app-icon></span>
        <span class="space-document-name">{{ document.name }}<small>{{ readiness(document) }}</small></span>
        <span class="row-arrow"><app-icon name="arrow"></app-icon></span>
      </button>
    </ng-template>
  `
})
export class KnowledgeSpacesComponent implements OnInit {
  @Input() agents: Agent[] = [];
  @Output() documentOpened = new EventEmitter<string>();
  @Output() chatStarted = new EventEmitter<{ agentId: string; entityId: string }>();
  @Output() spacesChanged = new EventEmitter<void>();

  readonly kindLabels = KIND_LABELS;
  readonly kindDescriptions = KIND_DESCRIPTIONS;
  readonly sharedKinds: Array<'reference' | 'operational'> = ['reference', 'operational'];
  spaces: KnowledgeSpace[] = [];
  customers: Customer[] = [];
  documents: SpaceDocument[] = [];
  expanded = new Set<string>();
  editing: { id: string | null; name: string; kind: 'reference' | 'operational'; description: string } | null = null;
  uploadingSpaceId: string | null = null;
  isLoading = true;
  error = '';

  constructor(private readonly http: HttpClient) {}
  ngOnInit(): void { this.load(); }

  load(): void {
    let pending = 3;
    const done = () => { if (--pending === 0) this.isLoading = false; };
    const fail = () => { this.error = 'Unable to load knowledge spaces.'; done(); };
    this.http.get<KnowledgeSpace[]>(`${API_URL}/knowledge/spaces`).subscribe({ next: items => { this.spaces = items; done(); }, error: fail });
    this.http.get<Customer[]>(`${API_URL}/entities`).subscribe({ next: items => { this.customers = items; done(); }, error: fail });
    this.http.get<SpaceDocument[]>(`${API_URL}/uploads`).subscribe({ next: items => { this.documents = items; done(); }, error: fail });
  }

  spacesOf(kind: string): KnowledgeSpace[] { return this.spaces.filter(space => space.kind === kind); }
  entityDocuments(entityId: string): SpaceDocument[] { return this.documents.filter(item => item.knowledgeKind === 'entity' && item.entityId === entityId && item.entityStatus === 'linked'); }
  unassigned(): SpaceDocument[] { return this.documents.filter(item => item.knowledgeKind === 'entity' && !(item.entityId && item.entityStatus === 'linked')); }
  spaceDocuments(spaceId: string): SpaceDocument[] { return this.documents.filter(item => item.spaceId === spaceId); }
  agentsUsing(spaceId: string): Agent[] { return this.agents.filter(agent => agent.spaceIds.includes(spaceId)); }
  customerAgents(): Agent[] { return this.agents.filter(agent => agent.customerAccess !== 'none'); }
  toggle(key: string): void { this.expanded.has(key) ? this.expanded.delete(key) : this.expanded.add(key); }
  initials(name: string): string { return name.split(/\s+/).filter(Boolean).slice(0, 2).map(word => word[0].toUpperCase()).join(''); }
  readiness(document: SpaceDocument): string {
    if (document.extractionStatus === 'unsupported') return 'Stored only · text extraction not supported';
    if (document.extractionStatus === 'failed') return 'Processing failed · not searchable';
    if (document.indexStatus === 'ready' || document.factStatus === 'ready') return 'Ready for agents';
    return 'Processing…';
  }

  startCreate(): void { this.editing = { id: null, name: '', kind: 'reference', description: '' }; }
  startEdit(space: KnowledgeSpace): void { this.editing = { id: space.id, name: space.name, kind: space.kind, description: space.description }; }
  saveSpace(): void {
    if (!this.editing) return;
    const { id, name, kind, description } = this.editing;
    const request = id
      ? this.http.patch<KnowledgeSpace>(`${API_URL}/knowledge/spaces/${id}`, { name, description })
      : this.http.post<KnowledgeSpace>(`${API_URL}/knowledge/spaces`, { name, kind, description });
    request.subscribe({
      next: space => { this.editing = null; this.expanded.add(space.id); this.load(); this.spacesChanged.emit(); },
      error: response => this.error = response.error?.detail || 'Could not save the space.'
    });
  }
  deleteSpace(space: KnowledgeSpace): void {
    if (!window.confirm(`Delete the space "${space.name}"? Agents that use it will lose access to it.`)) return;
    this.http.delete(`${API_URL}/knowledge/spaces/${space.id}`).subscribe({
      next: () => { this.load(); this.spacesChanged.emit(); },
      error: response => this.error = response.error?.detail || 'Could not delete the space.'
    });
  }
  uploadInto(space: KnowledgeSpace, event: Event): void {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    input.value = '';
    if (!files.length) return;
    this.uploadingSpaceId = space.id;
    this.error = '';
    const next = (index: number, failures: string[]): void => {
      if (index >= files.length) {
        this.uploadingSpaceId = null;
        if (failures.length) this.error = `Some files could not be uploaded: ${failures.join('; ')}`;
        this.load(); this.spacesChanged.emit();
        return;
      }
      const form = new FormData();
      form.append('file', files[index]);
      form.append('spaceId', space.id);
      this.http.post(`${API_URL}/uploads`, form).subscribe({
        next: () => next(index + 1, failures),
        error: response => { failures.push(`${files[index].name}: ${response.error?.detail || 'upload failed'}`); next(index + 1, failures); }
      });
    };
    next(0, []);
  }
}

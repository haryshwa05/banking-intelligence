import { IconComponent } from './icon.component';
import { CommonModule } from '@angular/common';
import { Component, EventEmitter, Input, OnDestroy, OnInit, Output } from '@angular/core';
import { HttpClient, HttpClientModule } from '@angular/common/http';
import { KIND_LABELS, KnowledgeKind, KnowledgeSpace } from './models';

type ExtractionStatus = 'queued' | 'processing' | 'completed' | 'failed' | 'unsupported';
type EntityStatus = 'queued' | 'processing' | 'linked' | 'needs_review' | 'failed';

interface UploadedDocument {
  id: string;
  name: string;
  type: string;
  sizeBytes: number;
  uploadedAt: string;
  url: string;
  extractionStatus: ExtractionStatus;
  indexStatus?: 'queued' | 'indexing' | 'ready' | 'failed' | null;
  factStatus?: 'queued' | 'processing' | 'ready' | 'failed' | null;
  entityId?: string | null;
  entityName?: string | null;
  entityStatus?: EntityStatus | null;
  entityReason?: string | null;
  knowledgeKind: KnowledgeKind;
  spaceId?: string | null;
  spaceName?: string | null;
}

interface Customer {
  id: string;
  name: string;
  documentCount: number;
}

interface DocumentExtraction {
  document: UploadedDocument;
  status: ExtractionStatus;
  text: string | null;
  pageCount: number | null;
  ocrPageCount: number | null;
  error: string | null;
}

interface DocumentFact {
  id: string;
  page_number: number;
  raw_label: string;
  raw_value: string;
  currency: string | null;
  value_type: string;
  confidence: number | null;
}

interface DocumentFacts {
  status: 'queued' | 'processing' | 'ready' | 'failed' | 'unavailable';
  error: string | null;
  facts: DocumentFact[];
}

@Component({
  selector: 'app-document-repository',
  standalone: true,
  imports: [IconComponent, CommonModule, HttpClientModule],
  template: `
      <section class="workspace repository-workspace">
        <div class="workspace-toolbar">
          <div><h1>Document Repository</h1><span class="document-count">{{ documents.length }} total</span></div>
          <label class="upload-button" for="file-input"><app-icon name="upload"></app-icon>Upload files</label>
          <input id="file-input" type="file" multiple accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt" (change)="selectFiles($event)">
        </div>

        <div class="upload-bar" [class.dragging]="isDragging" (dragover)="onDragOver($event)" (dragleave)="isDragging = false" (drop)="onDrop($event)">
          <span class="upload-symbol"><app-icon name="upload"></app-icon></span>
          <span class="upload-name">{{ uploadSelectionLabel() }}</span>
          <label class="upload-destination">File into
            <select aria-label="Knowledge destination" [value]="uploadSpaceId" (change)="uploadSpaceId = $any($event.target).value" [disabled]="isUploading">
              <option value="">Customer knowledge (detect customer)</option>
              <option *ngFor="let space of spaces" [value]="space.id">{{ space.name }} ({{ kindLabels[space.kind] }})</option>
            </select>
          </label>
          <span *ngIf="selectedFiles.length" class="upload-size">{{ formatBytes(selectedFilesSize()) }}</span>
          <button *ngIf="selectedFiles.length" type="button" class="submit-upload" [disabled]="isUploading" (click)="upload()">{{ isUploading ? 'Uploading ' + uploadProgress + ' of ' + selectedFiles.length : 'Upload ' + selectedFiles.length + (selectedFiles.length === 1 ? ' file' : ' files') }}</button>
        </div>
        <p *ngIf="error" class="error-banner">{{ error }}</p>

        <section class="document-table">
          <div class="table-heading"><span>Name</span><span>Type</span><span>Knowledge context</span><span>Status</span><span>Uploaded</span><span></span></div>
          <div *ngIf="isLoading" class="empty-table">Loading documents...</div>
          <div *ngIf="!isLoading && documents.length === 0" class="empty-table">No documents uploaded.</div>
          <button class="document-row" type="button" *ngFor="let document of documents" (click)="openDetails(document)">
            <span class="name-cell"><span class="file-icon" [class.image]="document.type.startsWith('image/')"><app-icon [name]="document.type.startsWith('image/') ? 'image' : 'file'"></app-icon></span><span class="document-name">{{ document.name }}<small>{{ formatBytes(document.sizeBytes) }}</small></span></span>
            <span class="document-type">{{ fileType(document.type) }}</span>
            <span class="document-owner" [class.review]="document.knowledgeKind === 'entity' && (document.entityStatus === 'needs_review' || document.entityStatus === 'failed')"><span [class]="'kind-dot kind-' + document.knowledgeKind" [title]="kindLabels[document.knowledgeKind]"></span>{{ knowledgeLabel(document) }}</span>
            <span class="status-badge" [class]="'status-' + document.extractionStatus">{{ statusLabel(document.extractionStatus) }}</span>
            <span class="document-date">{{ document.uploadedAt | date:'mediumDate' }}</span>
            <span class="row-arrow"><app-icon name="arrow"></app-icon></span>
          </button>
        </section>
      </section>

    <div *ngIf="activeDocument" class="detail-backdrop" (click)="closeDetails()">
      <aside class="detail-panel" (click)="$event.stopPropagation()">
        <div class="detail-topline"><span>Document details</span><button class="close-button" type="button" aria-label="Close" (click)="closeDetails()"><app-icon name="close"></app-icon></button></div>
        <h2>{{ activeDocument.name }}</h2>
        <div class="metadata"><span>{{ fileType(activeDocument.type) }}</span><span>{{ formatBytes(activeDocument.sizeBytes) }}</span><span>{{ activeDocument.uploadedAt | date:'mediumDate' }}</span></div>
        <section class="knowledge-state" [class]="'knowledge-state kind-' + activeDocument.knowledgeKind">
          <span>Knowledge context</span><strong>{{ kindLabels[activeDocument.knowledgeKind] }}{{ activeDocument.spaceName ? ' · ' + activeDocument.spaceName : '' }}</strong>
          <small>{{ activeDocument.knowledgeKind === 'entity' ? 'Customer-specific. Only used in chats about the customer who owns it.' : 'Shared. Used by every agent granted this space, for any customer.' }}</small>
          <div class="ownership-controls"><select aria-label="Move to knowledge space" [value]="moveSpaceId" (change)="moveSpaceId = $any($event.target).value" [disabled]="isMoving"><option value="">Customer knowledge</option><option *ngFor="let space of spaces" [value]="space.id">{{ space.name }} ({{ kindLabels[space.kind] }})</option></select><button type="button" (click)="moveKnowledge()" [disabled]="isMoving || moveSpaceId === (activeDocument.spaceId || '')">Move</button></div>
          <small *ngIf="knowledgeError" class="ownership-error">{{ knowledgeError }}</small>
        </section>
        <section *ngIf="activeDocument.knowledgeKind === 'entity'" class="ownership-state" [class.review]="activeDocument.entityStatus === 'needs_review' || activeDocument.entityStatus === 'failed'">
          <span>Customer ownership</span><strong>{{ entityLabel(activeDocument) }}</strong><small *ngIf="activeDocument.entityReason">{{ activeDocument.entityReason }}</small>
        </section>
        <div *ngIf="activeDocument.knowledgeKind === 'entity' && activeDocument.entityStatus !== 'queued' && activeDocument.entityStatus !== 'processing'" class="ownership-editor">
          <label for="assign-customer">Assign to customer</label>
          <input class="ownership-search" aria-label="Find existing customer" placeholder="Find existing customer" [value]="assignmentCustomerFilter" (input)="assignmentCustomerFilter = $any($event.target).value" [disabled]="isAssigning">
          <div class="ownership-controls"><select id="assign-customer" [value]="assignmentEntityId" (change)="assignmentEntityId = $any($event.target).value" [disabled]="isAssigning"><option value="">Choose existing customer</option><option *ngFor="let customer of filteredCustomers(assignmentCustomerFilter, assignmentEntityId)" [value]="customer.id">{{ customer.name }} ({{ customer.documentCount }})</option></select><button type="button" (click)="assignCustomer()" [disabled]="isAssigning || !assignmentEntityId || assignmentEntityId === activeDocument.entityId">Assign</button></div>
          <div class="ownership-controls"><input aria-label="New customer name" placeholder="Or create a new customer" [value]="newCustomerName" (input)="newCustomerName = $any($event.target).value" [disabled]="isAssigning"><button type="button" (click)="createAndAssignCustomer()" [disabled]="isAssigning || newCustomerName.trim().length < 2">Create &amp; assign</button></div>
          <small *ngIf="ownershipError" class="ownership-error">{{ ownershipError }}</small>
        </div>
        <div class="detail-actions"><a [href]="fileUrl(activeDocument)" target="_blank" rel="noopener">Open in new tab</a><button type="button" class="delete-button" [disabled]="isDeleting" (click)="deleteDocument()">{{ isDeleting ? 'Deleting...' : 'Delete' }}</button></div>

        <section class="original-preview">
          <div class="preview-heading">Original document</div>
          <iframe *ngIf="activeDocument.type === 'application/pdf'" class="pdf-preview" [src]="previewUrl(activeDocument)" [title]="activeDocument.name"></iframe>
          <img *ngIf="activeDocument.type.startsWith('image/')" class="image-preview" [src]="previewUrl(activeDocument)" [alt]="activeDocument.name">
          <div *ngIf="!canPreview(activeDocument)" class="preview-unavailable">Preview is available for PDFs and images.</div>
        </section>

        <div *ngIf="isDetailLoading" class="detail-state">Loading...</div>
        <section *ngIf="!isDetailLoading && extraction" class="extraction">
          <div class="extraction-bar"><span>Extracted text</span><span class="status-badge" [class]="'status-' + extraction.status">{{ statusLabel(extraction.status) }}</span></div>
          <div *ngIf="extraction.status === 'queued' || extraction.status === 'processing'" class="detail-state"><span class="spinner"></span>{{ extraction.status === 'queued' ? 'Waiting to process.' : 'Processing document.' }}</div>
          <div *ngIf="extraction.status === 'unsupported'" class="detail-state">Extraction is currently available for PDFs and images.</div>
          <div *ngIf="extraction.status === 'failed'" class="failure-state"><strong>Processing failed</strong><span>{{ extraction.error || 'Try again after confirming the backend is running.' }}</span><button type="button" (click)="retry()" [disabled]="isRetrying">{{ isRetrying ? 'Retrying...' : 'Retry extraction' }}</button></div>
          <pre *ngIf="extraction.status === 'completed'" class="extracted-text">{{ extraction.text || 'No readable text was found.' }}</pre>
        </section>
        <section *ngIf="extraction?.status === 'completed'" class="facts-panel">
          <div class="extraction-bar"><span>Structured facts</span><span *ngIf="documentFacts" class="status-badge" [class]="'status-' + documentFacts.status">{{ documentFacts.status }}</span></div>
          <div *ngIf="!documentFacts" class="detail-state">Preparing facts...</div>
          <div *ngIf="documentFacts?.status === 'queued' || documentFacts?.status === 'processing'" class="detail-state">Extracting page-level facts...</div>
          <div *ngIf="documentFacts?.status === 'failed'" class="failure-state"><span>{{ documentFacts?.error || 'Fact extraction could not finish.' }}</span><button type="button" (click)="retryFacts()">Retry facts</button></div>
          <div *ngIf="documentFacts?.status === 'ready' && !documentFacts?.facts?.length" class="detail-state">No labelled facts were found.<button type="button" (click)="retryFacts()">Retry facts</button></div>
          <div *ngIf="documentFacts?.status === 'ready' && documentFacts?.facts?.length" class="fact-list"><div *ngFor="let fact of documentFacts?.facts" class="fact-row"><span>{{ fact.raw_label }}<small>Page {{ fact.page_number }} &middot; {{ fact.value_type }}</small></span><strong>{{ fact.raw_value }}</strong></div></div>
        </section>
      </aside>
    </div>
  `
})
export class DocumentRepositoryComponent implements OnInit, OnDestroy {
  @Input() previewDocumentId: string | null = null;
  @Output() knowledgeChanged = new EventEmitter<void>();
  readonly kindLabels = KIND_LABELS;
  spaces: KnowledgeSpace[] = [];
  uploadSpaceId = '';
  moveSpaceId = '';
  isMoving = false;
  knowledgeError = '';
  private readonly apiUrl = 'http://localhost:8000';
  private pollingTimer: ReturnType<typeof setTimeout> | null = null;
  private listRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  documents: UploadedDocument[] = [];
  customers: Customer[] = [];
  selectedFiles: File[] = [];
  activeDocument: UploadedDocument | null = null;
  extraction: DocumentExtraction | null = null;
  documentFacts: DocumentFacts | null = null;
  isDragging = false;
  isUploading = false;
  uploadProgress = 0;
  isLoading = true;
  isDetailLoading = false;
  isRetrying = false;
  isDeleting = false;
  isAssigning = false;
  assignmentEntityId = '';
  assignmentCustomerFilter = '';
  newCustomerName = '';
  ownershipError = '';
  error = '';

  constructor(private readonly http: HttpClient) {}

  ngOnInit(): void { this.loadDocuments(); this.loadSpaces(); }
  private loadSpaces(): void { this.http.get<KnowledgeSpace[]>(`${this.apiUrl}/knowledge/spaces`).subscribe({ next: spaces => this.spaces = spaces }); }
  knowledgeLabel(document: UploadedDocument): string {
    return document.knowledgeKind === 'entity' ? this.entityLabel(document) : `${document.knowledgeKind === 'reference' ? 'Reference' : 'Operational'} · ${document.spaceName}`;
  }
  moveKnowledge(): void {
    if (!this.activeDocument || this.isMoving) return;
    const document = this.activeDocument;
    const target = this.spaces.find(space => space.id === this.moveSpaceId);
    const message = target
      ? `File "${document.name}" into ${target.name}? It becomes shared knowledge${document.entityName ? ` and is removed from ${document.entityName}'s documents` : ''}.`
      : `Move "${document.name}" back to customer knowledge? Its customer will be detected again.`;
    if (!window.confirm(message)) return;
    this.isMoving = true;
    this.knowledgeError = '';
    this.http.put<UploadedDocument>(`${this.apiUrl}/uploads/${document.id}/knowledge`, { spaceId: this.moveSpaceId || null }).subscribe({
      next: (updated) => {
        this.isMoving = false;
        if (this.activeDocument?.id === updated.id) this.activeDocument = updated;
        this.documents = this.documents.map((item) => item.id === updated.id ? updated : item);
        this.loadCustomers(); this.loadSpaces(); this.knowledgeChanged.emit();
        if (updated.entityStatus === 'queued') this.loadDocuments(true);
      },
      error: (response) => { this.isMoving = false; this.knowledgeError = response.error?.detail || 'Could not move the document.'; }
    });
  }
  ngOnDestroy(): void { this.clearPolling(); if (this.listRefreshTimer) clearTimeout(this.listRefreshTimer); }

  loadDocuments(silent = false): void {
    if (!silent) this.isLoading = true;
    this.http.get<UploadedDocument[]>(`${this.apiUrl}/uploads`).subscribe({
      next: (documents) => {
        this.documents = documents;
        this.isLoading = false;
        if (this.previewDocumentId && this.activeDocument?.id !== this.previewDocumentId) {
          const preview = documents.find((document) => document.id === this.previewDocumentId);
          if (preview) this.openDetails(preview);
        }
        this.loadCustomers();
        if (this.listRefreshTimer) clearTimeout(this.listRefreshTimer);
        if (documents.some((document) => document.extractionStatus === 'queued' || document.extractionStatus === 'processing' ||
            document.indexStatus === 'queued' || document.indexStatus === 'indexing' || document.factStatus === 'queued' ||
            document.factStatus === 'processing' || document.entityStatus === 'queued' || document.entityStatus === 'processing' ||
            (document.factStatus === 'ready' && document.knowledgeKind === 'entity' && !document.entityStatus))) {
          this.listRefreshTimer = setTimeout(() => this.loadDocuments(true), 5000);
        }
      },
      error: () => { this.error = 'Unable to connect to the document service.'; this.isLoading = false; }
    });
  }

  loadCustomers(): void {
    this.http.get<Customer[]>(`${this.apiUrl}/entities`).subscribe({
      next: (customers) => this.customers = customers,
      error: () => this.error = 'Unable to load customers.'
    });
  }

  selectFiles(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.setFiles(Array.from(input.files ?? []));
    input.value = '';
  }

  onDragOver(event: DragEvent): void { event.preventDefault(); this.isDragging = true; }
  onDrop(event: DragEvent): void { event.preventDefault(); this.isDragging = false; this.setFiles(Array.from(event.dataTransfer?.files ?? [])); }

  upload(): void {
    const files = this.selectedFiles.filter((file) => file.size <= 50 * 1024 * 1024);
    const rejected = this.selectedFiles.filter((file) => file.size > 50 * 1024 * 1024).map((file) => file.name);
    if (!files.length) { this.error = 'Each file must be 50 MB or smaller.'; return; }
    this.isUploading = true;
    this.uploadProgress = 0;
    this.error = rejected.length ? `Skipped over-50 MB file${rejected.length === 1 ? '' : 's'}: ${rejected.join(', ')}` : '';
    this.uploadNext(files, 0, []);
  }

  private uploadNext(files: File[], index: number, failures: string[]): void {
    if (index >= files.length) {
      this.isUploading = false;
      this.uploadProgress = 0;
      this.selectedFiles = [];
      if (this.uploadSpaceId) this.knowledgeChanged.emit();
      if (failures.length) this.error = `Some files could not be uploaded: ${failures.join('; ')}`;
      this.loadDocuments();
      return;
    }
    this.uploadProgress = index + 1;
    const formData = new FormData();
    formData.append('file', files[index]);
    if (this.uploadSpaceId) formData.append('spaceId', this.uploadSpaceId);
    this.http.post<UploadedDocument>(`${this.apiUrl}/uploads`, formData).subscribe({
      next: (document) => { this.documents = [document, ...this.documents]; this.uploadNext(files, index + 1, failures); },
      error: (response) => {
        failures.push(`${files[index].name}: ${response.error?.detail || 'upload failed'}`);
        this.uploadNext(files, index + 1, failures);
      }
    });
  }

  openDetails(document: UploadedDocument): void {
    this.clearPolling();
    this.activeDocument = document;
    this.extraction = null;
    this.documentFacts = null;
    this.assignmentEntityId = document.entityId || '';
    this.moveSpaceId = document.spaceId || '';
    this.knowledgeError = '';
    this.assignmentCustomerFilter = '';
    this.newCustomerName = '';
    this.ownershipError = '';
    this.loadExtraction();
  }

  closeDetails(): void { this.clearPolling(); this.activeDocument = null; this.extraction = null; this.documentFacts = null; }

  retry(): void {
    if (!this.activeDocument) return;
    this.isRetrying = true;
    this.http.post<{ status: ExtractionStatus }>(`${this.apiUrl}/uploads/${this.activeDocument.id}/extraction/retry`, {}).subscribe({
      next: () => { this.isRetrying = false; this.loadExtraction(); },
      error: (response) => { this.isRetrying = false; this.error = response.error?.detail || 'Retry could not be started.'; }
    });
  }

  deleteDocument(): void {
    if (!this.activeDocument || !window.confirm(`Delete "${this.activeDocument.name}"? This cannot be undone.`)) return;
    const documentId = this.activeDocument.id;
    this.isDeleting = true;
    this.http.delete(`${this.apiUrl}/uploads/${documentId}`).subscribe({
      next: () => { this.documents = this.documents.filter((document) => document.id !== documentId); this.isDeleting = false; this.closeDetails(); this.loadCustomers(); },
      error: (response) => { this.isDeleting = false; this.error = response.error?.detail || 'The document could not be deleted.'; }
    });
  }

  fileUrl(document: UploadedDocument): string { return `${this.apiUrl}${document.url}`; }
  previewUrl(document: UploadedDocument): string { return `${this.apiUrl}/uploads/${document.id}/preview`; }
  canPreview(document: UploadedDocument): boolean { return document.type === 'application/pdf' || document.type.startsWith('image/'); }
  statusLabel(status: ExtractionStatus): string { return ({ queued: 'Queued', processing: 'Processing', completed: 'Ready', failed: 'Failed', unsupported: 'Unsupported' })[status]; }
  entityLabel(document: UploadedDocument): string {
    if (document.entityStatus === 'linked') return document.entityName || 'Customer linked';
    if (document.entityStatus === 'needs_review') return 'Needs ownership review';
    if (document.entityStatus === 'failed') return 'Ownership resolution failed';
    if (document.entityStatus === 'queued' || document.entityStatus === 'processing') return 'Resolving customer…';
    return document.extractionStatus === 'unsupported' ? 'Not available' : 'Awaiting document facts';
  }
  filteredCustomers(filterText: string, selectedId: string): Customer[] {
    const filter = filterText.trim().toLowerCase();
    const matches = this.customers.filter((customer) => !filter || customer.name.toLowerCase().includes(filter)).slice(0, 50);
    const selected = this.customers.find((customer) => customer.id === selectedId);
    return selected && !matches.some((customer) => customer.id === selectedId) ? [selected, ...matches] : matches;
  }
  assignCustomer(): void {
    if (!this.activeDocument || !this.assignmentEntityId || this.isAssigning) return;
    if (this.activeDocument.entityId && this.activeDocument.entityId !== this.assignmentEntityId &&
        !window.confirm(`Change the customer for "${this.activeDocument.name}"?`)) return;
    this.saveAssignment(this.assignmentEntityId);
  }
  createAndAssignCustomer(): void {
    const name = this.newCustomerName.trim();
    if (!this.activeDocument || name.length < 2 || this.isAssigning) return;
    this.isAssigning = true;
    this.ownershipError = '';
    this.http.post<Customer>(`${this.apiUrl}/entities`, { name }).subscribe({
      next: (customer) => { this.isAssigning = false; this.saveAssignment(customer.id); },
      error: (response) => { this.isAssigning = false; this.ownershipError = response.error?.detail || 'Could not create customer.'; }
    });
  }
  private saveAssignment(entityId: string): void {
    if (!this.activeDocument) return;
    const documentId = this.activeDocument.id;
    this.isAssigning = true;
    this.ownershipError = '';
    this.http.put<UploadedDocument>(`${this.apiUrl}/uploads/${documentId}/entity`, { entityId }).subscribe({
      next: (document) => {
        this.isAssigning = false;
        this.assignmentEntityId = entityId;
        this.newCustomerName = '';
        if (this.activeDocument?.id === documentId) this.activeDocument = document;
        this.documents = this.documents.map((item) => item.id === documentId ? document : item);
        this.loadCustomers();
      },
      error: (response) => { this.isAssigning = false; this.ownershipError = response.error?.detail || 'Could not assign document.'; this.loadCustomers(); }
    });
  }
  fileType(type: string): string {
    if (type === 'application/pdf') return 'PDF';
    if (type.startsWith('image/')) return 'Image';
    return type.split('/').pop()?.toUpperCase() || 'Document';
  }
  formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB'];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)) - 1, units.length - 1);
    return `${(bytes / Math.pow(1024, index + 1)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
  }
  iconFor(type: string): string { return type.startsWith('image/') ? 'IMG' : type === 'application/pdf' ? 'PDF' : 'DOC'; }
  private loadExtraction(): void {
    if (!this.activeDocument) return;
    this.isDetailLoading = true;
    const documentId = this.activeDocument.id;
    this.http.get<DocumentExtraction>(`${this.apiUrl}/uploads/${documentId}/extraction`).subscribe({
      next: (extraction) => {
        if (this.activeDocument?.id !== documentId) return;
        this.extraction = extraction;
        this.activeDocument = extraction.document;
        this.documents = this.documents.map((document) => document.id === documentId ? extraction.document : document);
        this.isDetailLoading = false;
        if (extraction.status === 'completed') this.loadFacts(documentId);
        if (extraction.status === 'queued' || extraction.status === 'processing') this.pollingTimer = setTimeout(() => this.loadExtraction(), 2000);
      },
      error: () => { this.isDetailLoading = false; this.error = 'Unable to load document details.'; }
    });
  }

  uploadSelectionLabel(): string {
    if (!this.selectedFiles.length) return 'Drop files here or choose Upload files';
    return this.selectedFiles.length === 1 ? this.selectedFiles[0].name : `${this.selectedFiles.length} files selected`;
  }
  selectedFilesSize(): number { return this.selectedFiles.reduce((total, file) => total + file.size, 0); }
  private setFiles(files: File[]): void { this.selectedFiles = files; this.uploadProgress = 0; this.error = ''; }
  retryFacts(): void {
    if (!this.activeDocument) return;
    this.http.post<{ status: string }>(`${this.apiUrl}/uploads/${this.activeDocument.id}/facts/retry`, {}).subscribe({
      next: () => this.loadFacts(this.activeDocument!.id),
      error: (response) => this.error = response.error?.detail || 'Fact extraction could not be restarted.'
    });
  }
  private loadFacts(documentId: string): void {
    this.http.get<DocumentFacts>(`${this.apiUrl}/uploads/${documentId}/facts`).subscribe({
      next: (facts) => {
        if (this.activeDocument?.id !== documentId) return;
        this.documentFacts = facts;
        if (facts.status === 'queued' || facts.status === 'processing') this.pollingTimer = setTimeout(() => this.loadFacts(documentId), 2500);
        if (facts.status === 'ready' && this.activeDocument?.knowledgeKind === 'entity' && (this.activeDocument?.entityStatus === 'queued' || this.activeDocument?.entityStatus === 'processing' || !this.activeDocument?.entityStatus)) this.pollingTimer = setTimeout(() => this.loadExtraction(), 1500);
      }
    });
  }
  private clearPolling(): void { if (this.pollingTimer) clearTimeout(this.pollingTimer); this.pollingTimer = null; }
}

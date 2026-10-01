import { CommonModule } from '@angular/common';
import { HttpClient, HttpClientModule } from '@angular/common/http';
import { Component, EventEmitter, Input, OnChanges, OnDestroy, OnInit, Output, SimpleChanges } from '@angular/core';

export type ScopeMode = 'all' | 'customer' | 'documents';

export interface Conversation {
  id: string;
  title: string;
  scopeMode: ScopeMode;
  entityId: string | null;
  documentIds: string[];
  createdAt: string;
  updatedAt: string;
}

interface Source {
  documentId: string;
  filename: string;
  pageNumber: number;
  chunkId: string;
}

interface Message {
  id: string;
  turnId: string;
  role: 'user' | 'assistant';
  content: string;
  status: string;
  sources: Source[];
  mode: string | null;
  debug: Record<string, unknown> | null;
  createdAt: string;
}

interface ConversationDetail extends Conversation { messages: Message[]; }
interface Customer { id: string; name: string; documentCount: number; }
interface ScopeDocument {
  id: string; name: string; entityName?: string | null;
  indexStatus?: string | null; factStatus?: string | null;
}

@Component({
  selector: 'app-chat',
  standalone: true,
  imports: [CommonModule, HttpClientModule],
  template: `
    <section class="chat-page">
      <header class="chat-header">
        <div><h1>{{ conversation?.title || 'New chat' }}</h1><span>{{ conversation ? scopeLabel(conversation) : 'Choose a scope, then ask your documents' }}</span></div>
        <span class="chat-header-label">Document Intelligence</span>
      </header>

      <div class="chat-scroll" #chatScroll>
        <div class="chat-thread">
          <div *ngIf="isLoading" class="chat-empty">Loading conversation...</div>
          <div *ngIf="!isLoading && !messages.length" class="chat-empty"><span class="empty-mark">D</span><h2>Ask your documents</h2><p>Answers use evidence from the selected customer or documents, with sources you can inspect.</p></div>
          <ng-container *ngFor="let message of messages">
            <div *ngIf="message.role === 'user'" class="chat-message user-message">
              <div class="message-avatar">You</div>
              <div class="message-body"><p>{{ message.content }}</p><button *ngIf="message.status === 'failed' || message.status === 'interrupted'" type="button" class="retry-turn" (click)="retry(message)" [disabled]="isStreaming">{{ message.status === 'interrupted' ? 'Interrupted · Retry' : 'Failed · Retry' }}</button></div>
            </div>
            <div *ngIf="message.role === 'assistant'" class="chat-message assistant-message">
              <div class="message-avatar assistant-avatar">D</div>
              <div class="message-body">
                <p class="assistant-copy" [class.provisional]="message.status === 'streaming'">{{ message.content }}<span *ngIf="message.status === 'streaming'" class="stream-cursor"></span></p>
                <div *ngIf="message.sources.length" class="chat-sources"><button type="button" *ngFor="let source of message.sources" (click)="sourceOpened.emit(source.documentId)">{{ source.filename }} · p. {{ source.pageNumber }}</button></div>
                <div *ngIf="message.debug" class="chat-trace"><button type="button" (click)="expandedTraceId = expandedTraceId === message.id ? null : message.id">{{ expandedTraceId === message.id ? 'Hide' : 'Show' }} developer trace</button><span>{{ message.mode }} route</span></div>
                <section *ngIf="message.debug && expandedTraceId === message.id" class="developer-trace" aria-label="Developer trace"><div class="trace-header"><strong>Developer trace</strong><small>Local retrieval and final evidence path</small></div><div class="trace-overview"><div><small>Route</small><strong>{{ traceValue(message, 'route') }}</strong></div><div><small>Scope</small><strong>{{ traceValue(message, 'documentScope') }}</strong></div><div><small>Final evidence</small><strong>{{ traceValue(message, 'finalEvidenceIds') || traceValue(message, 'claudeValidatedCitationIds') || 'None' }}</strong></div></div><details open><summary>Structured facts</summary><pre>{{ traceValue(message, 'structuredFacts') | json }}</pre></details><details><summary>RAG retrieval</summary><pre>{{ traceValue(message, 'rag') | json }}</pre></details><details><summary>Complete trace</summary><pre>{{ message.debug | json }}</pre></details><p>Contains excerpts and financial data. Use only in a trusted environment.</p></section>
              </div>
            </div>
          </ng-container>
          <div *ngIf="isStreaming && !hasStreamingAssistant()" class="chat-message assistant-message"><div class="message-avatar assistant-avatar">D</div><div class="message-body"><p class="chat-stage"><span class="stage-spinner"></span>{{ stageLabel }}</p></div></div>
          <p *ngIf="error" class="chat-error">{{ error }}</p>
        </div>
      </div>

      <div class="chat-composer-wrap">
        <div *ngIf="!conversation" class="chat-scope-setup">
          <label for="chat-scope">Answer from</label>
          <select id="chat-scope" [value]="scopeMode" (change)="scopeMode = $any($event.target).value"><option value="all">Automatic scope</option><option value="customer">One customer</option><option value="documents">Selected documents</option></select>
          <ng-container *ngIf="scopeMode === 'customer'"><input aria-label="Find customer" placeholder="Find customer" [value]="customerFilter" (input)="customerFilter = $any($event.target).value"><select aria-label="Select customer" [value]="selectedEntityId" (change)="selectedEntityId = $any($event.target).value"><option value="">Choose customer</option><option *ngFor="let customer of filteredCustomers()" [value]="customer.id">{{ customer.name }} ({{ customer.documentCount }})</option></select></ng-container>
          <div *ngIf="scopeMode === 'documents'" class="chat-document-picker"><div><input aria-label="Find documents" placeholder="Find documents" [value]="documentFilter" (input)="documentFilter = $any($event.target).value"><span>{{ selectedDocumentIds.length }} selected</span></div><div class="chat-document-options"><label *ngFor="let document of filteredDocuments()"><input type="checkbox" [checked]="selectedDocumentIds.includes(document.id)" [disabled]="!canUseDocument(document)" (change)="toggleDocument(document.id, $any($event.target).checked)"><span>{{ document.name }}<small>{{ canUseDocument(document) ? (document.entityName || 'Unassigned') : 'Processing' }}</small></span></label></div></div>
        </div>
        <div class="chat-composer"><textarea aria-label="Message your documents" rows="2" placeholder="Ask a question about your documents..." [value]="draft" (input)="draft = $any($event.target).value" (keydown)="onComposerKeydown($event)" [disabled]="isStreaming || isCreating"></textarea><button type="button" [disabled]="!canSend() && !isStreaming" (click)="isStreaming ? stop() : send()">{{ isStreaming ? 'Stop' : 'Send' }}</button></div>
        <small class="composer-hint">Answers are grounded in retrieved document evidence. Enter to send · Shift+Enter for a new line.</small>
      </div>
    </section>
  `
})
export class ChatComponent implements OnInit, OnChanges, OnDestroy {
  @Input() conversationId: string | null = null;
  @Output() conversationChanged = new EventEmitter<Conversation>();
  @Output() sourceOpened = new EventEmitter<string>();

  private readonly apiUrl = 'http://localhost:8000';
  private abortController: AbortController | null = null;
  conversation: Conversation | null = null;
  messages: Message[] = [];
  customers: Customer[] = [];
  documents: ScopeDocument[] = [];
  scopeMode: ScopeMode = 'all';
  selectedEntityId = '';
  selectedDocumentIds: string[] = [];
  customerFilter = '';
  documentFilter = '';
  draft = '';
  error = '';
  stageLabel = '';
  isLoading = false;
  isCreating = false;
  isStreaming = false;
  expandedTraceId: string | null = null;

  constructor(private readonly http: HttpClient) {}

  ngOnInit(): void { this.loadScopeOptions(); if (this.conversationId) this.loadConversation(this.conversationId); }
  ngOnChanges(changes: SimpleChanges): void {
    if (!changes['conversationId'] || changes['conversationId'].firstChange) return;
    if (this.conversation?.id === this.conversationId) return;
    this.stop();
    this.error = '';
    this.expandedTraceId = null;
    if (this.conversationId) this.loadConversation(this.conversationId);
    else { this.conversation = null; this.messages = []; this.draft = ''; this.scopeMode = 'all'; this.selectedEntityId = ''; this.selectedDocumentIds = []; }
  }
  ngOnDestroy(): void { this.stop(); }

  private loadScopeOptions(): void {
    this.http.get<Customer[]>(`${this.apiUrl}/entities`).subscribe({ next: rows => this.customers = rows });
    this.http.get<ScopeDocument[]>(`${this.apiUrl}/uploads`).subscribe({ next: rows => this.documents = rows });
  }
  private loadConversation(id: string): void {
    this.isLoading = true;
    this.http.get<ConversationDetail>(`${this.apiUrl}/conversations/${id}`).subscribe({
      next: detail => {
        if (this.conversationId !== id) return;
        this.conversation = detail;
        this.messages = detail.messages;
        this.isLoading = false;
        this.scrollToBottom();
      },
      error: () => { this.isLoading = false; this.error = 'Could not load this conversation.'; }
    });
  }
  scopeLabel(conversation: Conversation): string {
    if (conversation.scopeMode === 'all') return 'Automatic document scope';
    if (conversation.scopeMode === 'documents') return `${conversation.documentIds.length} selected document${conversation.documentIds.length === 1 ? '' : 's'}`;
    return this.customers.find(item => item.id === conversation.entityId)?.name || 'Selected customer';
  }
  filteredCustomers(): Customer[] {
    const term = this.customerFilter.trim().toLowerCase();
    return this.customers.filter(item => !term || item.name.toLowerCase().includes(term)).slice(0, 50);
  }
  filteredDocuments(): ScopeDocument[] {
    const term = this.documentFilter.trim().toLowerCase();
    return this.documents.filter(item => !term || item.name.toLowerCase().includes(term) || item.entityName?.toLowerCase().includes(term)).slice(0, 100);
  }
  canUseDocument(document: ScopeDocument): boolean { return document.indexStatus === 'ready' || document.factStatus === 'ready'; }
  toggleDocument(id: string, checked: boolean): void {
    this.selectedDocumentIds = checked ? [...new Set([...this.selectedDocumentIds, id])] : this.selectedDocumentIds.filter(item => item !== id);
  }
  canSend(): boolean {
    return !!this.draft.trim() && !this.isStreaming && !this.isCreating &&
      (this.conversation !== null || this.scopeMode === 'all' || (this.scopeMode === 'customer' && !!this.selectedEntityId) || (this.scopeMode === 'documents' && this.selectedDocumentIds.length > 0));
  }
  onComposerKeydown(event: KeyboardEvent): void { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); this.send(); } }
  hasStreamingAssistant(): boolean { return this.messages.some(item => item.role === 'assistant' && item.status === 'streaming'); }
  traceValue(message: Message, key: string): unknown { return message.debug?.[key] ?? null; }

  async send(): Promise<void> {
    if (!this.canSend()) return;
    const question = this.draft.trim();
    this.draft = '';
    this.error = '';
    try {
      if (!this.conversation) {
        this.isCreating = true;
        const payload: Record<string, unknown> = { scopeMode: this.scopeMode };
        if (this.scopeMode === 'customer') payload['entityId'] = this.selectedEntityId;
        if (this.scopeMode === 'documents') payload['documentIds'] = this.selectedDocumentIds;
        const response = await fetch(`${this.apiUrl}/conversations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        if (!response.ok) throw new Error(await this.responseError(response));
        this.conversation = await response.json() as Conversation;
        this.conversationChanged.emit(this.conversation);
      }
      await this.streamTurn(question, crypto.randomUUID());
    } catch (error) {
      this.error = error instanceof Error ? error.message : 'Could not start the conversation.';
      this.draft = question;
    } finally { this.isCreating = false; }
  }

  retry(message: Message): void { if (!this.isStreaming) void this.streamTurn(message.content, message.turnId, true); }
  stop(): void { this.abortController?.abort(); this.abortController = null; this.isStreaming = false; }

  private async streamTurn(question: string, turnId: string, retry = false): Promise<void> {
    if (!this.conversation || this.isStreaming) return;
    this.isStreaming = true;
    this.error = '';
    this.stageLabel = 'Starting...';
    if (!retry) this.messages.push({ id: `local-${turnId}`, turnId, role: 'user', content: question, status: 'pending', sources: [], mode: null, debug: null, createdAt: new Date().toISOString() });
    const draftId = `draft-${turnId}`;
    let completed = false;
    let draftMessage: Message | null = null;
    const controller = new AbortController();
    this.abortController = controller;
    this.scrollToBottom();
    try {
      const response = await fetch(`${this.apiUrl}/conversations/${this.conversation.id}/messages/stream`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, turnId }), signal: controller.signal,
      });
      if (!response.ok) throw new Error(await this.responseError(response));
      if (!response.body) throw new Error('The server did not return a stream.');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
        let boundary: number;
        while ((boundary = buffer.indexOf('\n\n')) !== -1) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const event = block.match(/^event: (.+)$/m)?.[1];
          const data = block.match(/^data: (.+)$/m)?.[1];
          if (!event || !data) continue;
          const payload = JSON.parse(data);
          if (event === 'status') this.stageLabel = payload.label;
          if (event === 'delta') {
            if (!draftMessage) {
              draftMessage = { id: draftId, turnId, role: 'assistant', content: '', status: 'streaming', sources: [], mode: null, debug: null, createdAt: new Date().toISOString() };
              this.messages.push(draftMessage);
            }
            draftMessage!.content += payload.text;
            this.scrollToBottom();
          }
          if (event === 'final') {
            completed = true;
            this.messages = this.messages.filter(item => item.id !== draftId);
            const user = this.messages.find(item => item.turnId === turnId && item.role === 'user');
            if (user) user.status = 'completed';
            this.messages.push(payload.message as Message);
            this.conversation = payload.conversation as Conversation;
            this.conversationChanged.emit(this.conversation);
            this.scrollToBottom();
          }
          if (event === 'error') throw new Error(payload.detail || 'The answer could not be completed.');
        }
      }
      if (!completed) throw new Error('The connection ended before the answer was complete.');
    } catch (error) {
      this.messages = this.messages.filter(item => item.id !== draftId);
      const user = this.messages.find(item => item.turnId === turnId && item.role === 'user');
      if (user) user.status = controller.signal.aborted ? 'interrupted' : 'failed';
      if (!controller.signal.aborted) this.error = error instanceof Error ? error.message : 'The answer could not be completed.';
    } finally {
      if (this.abortController === controller) this.abortController = null;
      this.isStreaming = false;
      this.stageLabel = '';
    }
  }

  private async responseError(response: Response): Promise<string> {
    try { const body = await response.json(); return body.detail || `Request failed (${response.status}).`; }
    catch { return `Request failed (${response.status}).`; }
  }
  private scrollToBottom(): void { setTimeout(() => document.querySelector('.chat-scroll')?.scrollTo({ top: document.querySelector('.chat-scroll')?.scrollHeight || 0, behavior: 'smooth' }), 30); }
}

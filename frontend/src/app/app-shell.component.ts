import { CommonModule } from '@angular/common';
import { HttpClient, HttpClientModule } from '@angular/common/http';
import { Component, OnDestroy, OnInit } from '@angular/core';
import { ChatComponent, Conversation } from './chat.component';
import { DocumentRepositoryComponent } from './document-repository.component';
import { AppLogoComponent } from './app-logo.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, HttpClientModule, ChatComponent, DocumentRepositoryComponent, AppLogoComponent],
  template: `
    <div class="application-layout">
      <div *ngIf="sidebarOpen" class="sidebar-scrim" (click)="sidebarOpen = false"></div>
      <aside class="app-sidebar" [class.open]="sidebarOpen" aria-label="Main navigation">
        <div class="sidebar-brand"><app-logo class="product-mark"></app-logo><div><strong>Document Intelligence</strong><small>Local workspace</small></div></div>
        <button class="new-chat-button" type="button" (click)="newChat()"><span>＋</span> New chat</button>
        <button class="sidebar-nav-item" type="button" [class.active]="view === 'repository'" (click)="openRepository()"><span class="sidebar-nav-icon">▣</span> Document Repository</button>
        <div class="sidebar-section-label">Recent chats</div>
        <div class="sidebar-conversations">
          <div *ngFor="let item of conversations" class="conversation-entry" [class.active]="view === 'chat' && activeConversationId === item.id">
            <button type="button" class="conversation-open" (click)="openConversation(item.id)" [title]="item.title">{{ item.title }}</button>
            <button type="button" class="conversation-menu" aria-label="Rename conversation" title="Rename" (click)="renameConversation(item)">✎</button>
            <button type="button" class="conversation-menu" aria-label="Delete conversation" title="Delete" (click)="deleteConversation(item)">×</button>
          </div>
          <button *ngIf="hasMore" class="load-more-chats" type="button" (click)="loadMore()">Load more chats</button>
        </div>
        <div class="sidebar-footer"><span class="local-dot"></span> Local document storage</div>
      </aside>

      <main class="main-surface" [class.repository-view]="view === 'repository'">
        <div class="mobile-bar"><button type="button" aria-label="Open menu" (click)="sidebarOpen = true">☰</button><app-logo class="product-mark"></app-logo><strong>Document Intelligence</strong></div>
        <app-chat *ngIf="view === 'chat'" [conversationId]="activeConversationId" [displayTitle]="activeConversationTitle()" (conversationChanged)="conversationChanged($event)" (sourceOpened)="openSource($event)" (repositoryOpened)="openRepository()"></app-chat>
        <app-document-repository *ngIf="view === 'repository'" [previewDocumentId]="previewDocumentId"></app-document-repository>
      </main>
    </div>
  `
})
export class AppShellComponent implements OnInit, OnDestroy {
  private readonly apiUrl = 'http://localhost:8000';
  private readonly pageSize = 50;
  view: 'chat' | 'repository' = 'chat';
  activeConversationId: string | null = null;
  previewDocumentId: string | null = null;
  conversations: Conversation[] = [];
  hasMore = false;
  sidebarOpen = false;

  constructor(private readonly http: HttpClient) {}
  ngOnInit(): void { window.addEventListener('hashchange', this.onHashChange); this.onHashChange(); this.refreshConversations(); }
  ngOnDestroy(): void { window.removeEventListener('hashchange', this.onHashChange); }

  private onHashChange = (): void => {
    const hash = window.location.hash;
    if (hash === '#/documents') { this.view = 'repository'; this.activeConversationId = null; return; }
    const match = /^#\/chat\/([a-f0-9-]+)$/.exec(hash);
    this.view = 'chat';
    this.activeConversationId = match?.[1] || null;
    this.previewDocumentId = null;
  };
  private navigate(hash: string): void { window.location.hash = hash; this.onHashChange(); this.sidebarOpen = false; }
  activeConversationTitle(): string | null { return this.conversations.find(item => item.id === this.activeConversationId)?.title || null; }
  newChat(): void { this.navigate('#/chat'); }
  openConversation(id: string): void { this.navigate(`#/chat/${id}`); }
  openRepository(): void { this.previewDocumentId = null; this.navigate('#/documents'); }
  openSource(documentId: string): void { this.previewDocumentId = documentId; this.navigate('#/documents'); }

  refreshConversations(): void {
    this.http.get<Conversation[]>(`${this.apiUrl}/conversations?limit=${this.pageSize}&offset=0`).subscribe({
      next: items => { this.conversations = items; this.hasMore = items.length === this.pageSize; }
    });
  }
  loadMore(): void {
    this.http.get<Conversation[]>(`${this.apiUrl}/conversations?limit=${this.pageSize}&offset=${this.conversations.length}`).subscribe({
      next: items => { this.conversations = [...this.conversations, ...items]; this.hasMore = items.length === this.pageSize; }
    });
  }
  conversationChanged(conversation: Conversation): void {
    this.activeConversationId = conversation.id;
    if (window.location.hash !== `#/chat/${conversation.id}`) window.history.replaceState(null, '', `#/chat/${conversation.id}`);
    this.conversations = [conversation, ...this.conversations.filter(item => item.id !== conversation.id)];
  }
  renameConversation(conversation: Conversation): void {
    const title = window.prompt('Conversation title', conversation.title)?.trim();
    if (!title || title === conversation.title) return;
    this.http.patch<Conversation>(`${this.apiUrl}/conversations/${conversation.id}`, { title }).subscribe({
      next: updated => this.conversations = this.conversations.map(item => item.id === updated.id ? updated : item)
    });
  }
  deleteConversation(conversation: Conversation): void {
    if (!window.confirm(`Delete chat "${conversation.title}"? This cannot be undone.`)) return;
    this.http.delete(`${this.apiUrl}/conversations/${conversation.id}`).subscribe({
      next: () => {
        this.conversations = this.conversations.filter(item => item.id !== conversation.id);
        if (this.activeConversationId === conversation.id) this.newChat();
      }
    });
  }
}

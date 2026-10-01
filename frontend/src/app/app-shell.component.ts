import { CommonModule } from '@angular/common';
import { HttpClient, HttpClientModule } from '@angular/common/http';
import { Component, OnDestroy, OnInit } from '@angular/core';
import { ChatComponent, Conversation } from './chat.component';
import { DocumentRepositoryComponent } from './document-repository.component';
import { AppLogoComponent } from './app-logo.component';
import { IconComponent } from './icon.component';
import { LoginComponent } from './login.component';
import { AccountMenuComponent } from './account-menu.component';
import { KnowledgeSpacesComponent } from './knowledge-spaces.component';
import { AgentBuilderComponent } from './agent-builder.component';
import { API_URL, Agent, Customer, KnowledgeSpace } from './models';

type View = 'home' | 'chat' | 'knowledge' | 'repository' | 'builder';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [LoginComponent, AccountMenuComponent, IconComponent, CommonModule, HttpClientModule, ChatComponent, DocumentRepositoryComponent, AppLogoComponent, KnowledgeSpacesComponent, AgentBuilderComponent],
  template: `
    <app-login *ngIf="!isSignedIn" (signedIn)="signIn()"></app-login>
    <div *ngIf="isSignedIn" class="application-layout" [class.sidebar-collapsed]="sidebarCollapsed">
      <div *ngIf="sidebarOpen" class="sidebar-scrim" (click)="sidebarOpen = false"></div>
      <aside id="main-sidebar" class="app-sidebar" [class.open]="sidebarOpen" aria-label="Main navigation">
        <div class="sidebar-brand">
          <button type="button" class="brand-home" (click)="navigate('#/')" aria-label="Banking Intelligence home"><app-logo class="product-mark"></app-logo><span>Banking Intelligence</span></button>
          <button type="button" class="icon-button mobile-close" aria-label="Close menu" (click)="sidebarOpen = false"><app-icon name="close"></app-icon></button>
        </div>
        <div class="sidebar-search"><app-icon name="search"></app-icon><input aria-label="Search agents and chats" placeholder="Search" [value]="sidebarQuery" (input)="sidebarQuery = $any($event.target).value"><button *ngIf="sidebarQuery" class="icon-button" type="button" aria-label="Clear search" (click)="sidebarQuery = ''"><app-icon name="close"></app-icon></button></div>
        <nav class="workspace-navigation" aria-label="Workspace">
          <button class="sidebar-nav-item" type="button" [class.active]="view === 'home'" [attr.aria-current]="view === 'home' ? 'page' : null" (click)="navigate('#/')" data-tooltip="All agents" aria-label="All agents"><app-icon name="home"></app-icon><span>All agents</span></button>
          <button class="sidebar-nav-item" type="button" [class.active]="view === 'knowledge'" [attr.aria-current]="view === 'knowledge' ? 'page' : null" (click)="navigate('#/knowledge')" data-tooltip="Knowledge Spaces" aria-label="Knowledge Spaces"><app-icon name="folder"></app-icon><span>Knowledge Spaces</span></button>
          <button class="sidebar-nav-item" type="button" [class.active]="view === 'repository'" [attr.aria-current]="view === 'repository' ? 'page' : null" (click)="openRepository()" data-tooltip="Document Repository" aria-label="Document Repository"><app-icon name="file"></app-icon><span>Document Repository</span></button>
          <button class="sidebar-nav-item" type="button" [class.active]="view === 'builder'" [attr.aria-current]="view === 'builder' ? 'page' : null" (click)="navigate('#/builder')" data-tooltip="Agent Builder" aria-label="Agent Builder"><app-icon name="workflow"></app-icon><span>Agent Builder</span></button>
        </nav>
        <div *ngIf="!sidebarCollapsed" class="sidebar-scroll">
          <div class="sidebar-section-label"><span><app-icon name="down"></app-icon> Agents</span><button type="button" class="icon-button" aria-label="Create agent" title="Create agent" (click)="navigate('#/builder/new')"><app-icon name="plus"></app-icon></button></div>
          <div *ngFor="let agent of sidebarAgents()" class="agent-group" [class.active]="activeAgentId === agent.id">
            <button type="button" class="agent-nav" [class.active]="view === 'chat' && activeAgentId === agent.id && (!activeConversationId || sidebarCollapsed)" (click)="newAgentChat(agent.id)" [attr.data-tooltip]="agent.name" [attr.aria-label]="agent.name">
              <app-icon name="chat"></app-icon><span class="agent-nav-name">{{ agent.name }}</span><app-icon class="agent-new" name="plus"></app-icon>
            </button>
            <div *ngIf="activeAgentId === agent.id || expandedAgentIds.has(agent.id) || sidebarQuery" class="agent-chats">
              <div *ngFor="let item of sidebarChats(agent.id)" class="conversation-entry" [class.active]="view === 'chat' && activeConversationId === item.id">
                <button type="button" class="conversation-open" (click)="openConversation(item)" [title]="item.title"><span>{{ item.title }}</span><small>{{ customerName(item.entityId) || 'No customer' }}</small></button>
                <button type="button" class="conversation-menu" aria-label="Rename conversation" title="Rename" (click)="renameConversation(item)"><app-icon name="edit"></app-icon></button>
                <button type="button" class="conversation-menu" aria-label="Delete conversation" title="Delete" (click)="deleteConversation(item)"><app-icon name="trash"></app-icon></button>
              </div>
              <p *ngIf="!chatsFor(agent.id).length" class="agent-chats-empty">No chats yet</p>
            </div>
            <button *ngIf="activeAgentId !== agent.id && chatsFor(agent.id).length" type="button" class="agent-expand" (click)="toggleAgent(agent.id)">{{ expandedAgentIds.has(agent.id) ? 'Hide' : 'Show' }} {{ chatsFor(agent.id).length }} chat{{ chatsFor(agent.id).length === 1 ? '' : 's' }}</button>
          </div>
          <p *ngIf="sidebarQuery && !sidebarAgents().length" class="sidebar-search-empty">No matching agents or chats.</p>
          <ng-container *ngIf="legacyChats().length">
            <button type="button" class="sidebar-section-label legacy-toggle" (click)="showLegacy = !showLegacy"><span><app-icon [name]="showLegacy ? 'down' : 'chevron'"></app-icon> Earlier chats ({{ legacyChats().length }})</span></button>
            <ng-container *ngIf="showLegacy || sidebarQuery">
              <div *ngFor="let item of sidebarChats(null)" class="conversation-entry" [class.active]="view === 'chat' && activeConversationId === item.id">
                <button type="button" class="conversation-open" (click)="openConversation(item)" [title]="item.title"><span>{{ item.title }}</span><small>Before agents</small></button>
                <button type="button" class="conversation-menu" aria-label="Delete conversation" title="Delete" (click)="deleteConversation(item)"><app-icon name="trash"></app-icon></button>
              </div>
            </ng-container>
          </ng-container>
        </div>
        <div *ngIf="sidebarCollapsed && !sidebarOpen" class="sidebar-agent-shortcut">
          <button type="button" class="sidebar-nav-item" [class.active]="view === 'chat'" aria-label="Expand agents and chats" data-tooltip="Agents &amp; chats" aria-controls="main-sidebar" (click)="sidebarCollapsed = false"><app-icon name="chat"></app-icon></button>
        </div>
        <div class="sidebar-footer"><span class="storage-label"><span class="local-dot"></span> Local document storage</span><button type="button" class="icon-button sidebar-collapse" [attr.aria-label]="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'" [attr.aria-expanded]="!sidebarCollapsed" aria-controls="main-sidebar" [attr.data-tooltip]="sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'" (click)="sidebarCollapsed = !sidebarCollapsed"><app-icon name="panel"></app-icon></button></div>
      </aside>

      <main class="main-surface" [class.repository-view]="view !== 'chat'">
        <header class="surface-topbar">
          <button type="button" class="icon-button mobile-menu" aria-label="Open menu" (click)="sidebarCollapsed = false; sidebarOpen = true"><app-icon name="menu"></app-icon></button>
          <nav class="breadcrumbs" aria-label="Breadcrumb"><app-icon [name]="viewIcon()"></app-icon><app-icon class="breadcrumb-chevron" name="chevron"></app-icon><button type="button" (click)="navigate('#/')">Workspace</button><app-icon class="breadcrumb-chevron" name="chevron"></app-icon><span aria-current="page">{{ viewLabel() }}</span></nav>
          <app-account-menu (loggedOut)="logout()"></app-account-menu>
        </header>
        <div class="surface-content">
        <section *ngIf="view === 'home'" class="workspace agent-home">
          <div class="workspace-toolbar"><div><h1>Agents</h1><span class="document-count">Each agent has a role, its own chats, and access only to the knowledge listed on its card.</span></div><button type="button" class="upload-button" (click)="navigate('#/builder/new')"><app-icon name="plus"></app-icon>Create agent</button></div>
          <div class="view-controls"><div class="segmented-control" aria-label="Agent views"><button type="button" class="selected" aria-current="page">All agents <span class="count-pill">{{ agents.length }}</span></button><button type="button" (click)="navigate('#/builder')">Manage agents</button></div></div>
          <div class="agent-board"><div class="board-heading"><span class="board-accent"></span><span>Your agents</span><span class="count-pill">{{ agents.length }}</span><button type="button" class="icon-button" aria-label="Add agent" (click)="navigate('#/builder/new')"><app-icon name="plus"></app-icon></button></div>
          <div class="agent-card-grid">
            <article *ngFor="let agent of agents" class="agent-card">
              <div class="agent-card-topline"><span>{{ agent.builtIn ? 'Built-in agent' : 'Custom agent' }}</span><app-icon name="agent"></app-icon></div>
              <header><div><h2>{{ agent.name }}</h2><p>{{ agent.purpose }}</p></div></header>
              <div class="agent-card-section"><small>Knowledge access</small>
                <div class="knowledge-chips">
                  <span *ngIf="agent.customerAccess !== 'none'" class="knowledge-chip kind-entity">Selected customer's documents{{ agent.customerAccess === 'optional' ? ' (optional)' : '' }}</span>
                  <span *ngFor="let space of spacesFor(agent)" class="knowledge-chip" [class]="'knowledge-chip kind-' + space.kind">{{ space.name }}</span>
                </div>
              </div>
              <footer><span class="card-chat-count"><app-icon name="chat"></app-icon>{{ agent.conversationCount }} chat{{ agent.conversationCount === 1 ? '' : 's' }}</span><div><button type="button" class="link-button" (click)="navigate('#/builder/' + agent.id)">View setup</button><button type="button" class="primary-button" (click)="newAgentChat(agent.id)">Start chat</button></div></footer>
            </article>
          </div>
          </div>
        </section>

        <div *ngIf="view === 'chat' && activeAgentId && !activeAgent()" class="chat-empty">{{ agentsLoaded ? "This agent no longer exists." : "Loading agent…" }}</div>
        <app-chat *ngIf="view === 'chat' && (!activeAgentId || activeAgent())"[conversationId]="activeConversationId" [agent]="activeAgent()" [initialEntityId]="initialEntityId" [spaces]="spaces" [displayTitle]="activeConversationTitle()" (conversationChanged)="conversationChanged($event)" (sourceOpened)="openSource($event)" (repositoryOpened)="openRepository()"></app-chat>
        <app-knowledge-spaces *ngIf="view === 'knowledge'" [agents]="agents" (documentOpened)="openSource($event)" (chatStarted)="startCustomerChat($event.agentId, $event.entityId)" (spacesChanged)="loadSpaces()"></app-knowledge-spaces>
        <app-document-repository *ngIf="view === 'repository'" [previewDocumentId]="previewDocumentId" (knowledgeChanged)="loadSpaces()"></app-document-repository>
        <app-agent-builder *ngIf="view === 'builder'" [editAgentId]="builderAgentId" (agentsChanged)="loadAgents()" (chatStarted)="newAgentChat($event)" (closed)="navigate('#/builder')"></app-agent-builder>
        </div>
      </main>
    </div>
  `
})
export class AppShellComponent implements OnInit, OnDestroy {
  // Deliberately in memory: every application load starts at the prototype login.
  isSignedIn = false;
  view: View = 'home';
  activeAgentId: string | null = null;
  activeConversationId: string | null = null;
  initialEntityId: string | null = null;
  builderAgentId: string | null = null;
  previewDocumentId: string | null = null;
  agents: Agent[] = [];
  agentsLoaded = false;
  spaces: KnowledgeSpace[] = [];
  customers: Customer[] = [];
  conversations: Conversation[] = [];
  expandedAgentIds = new Set<string>();
  showLegacy = false;
  sidebarOpen = false;
  sidebarCollapsed = false;
  sidebarQuery = '';

  viewLabel(): string { return { home: 'Agents', chat: 'Chat', knowledge: 'Knowledge Spaces', repository: 'Documents', builder: 'Agent Builder' }[this.view]; }
  viewIcon(): string { return { home: 'users', chat: 'chat', knowledge: 'folder', repository: 'file', builder: 'workflow' }[this.view]; }
  sidebarAgents(): Agent[] {
    const query = this.sidebarQuery.trim().toLowerCase();
    return this.agents.filter(agent => !query || agent.name.toLowerCase().includes(query) || this.chatsFor(agent.id).some(chat => chat.title.toLowerCase().includes(query)));
  }
  sidebarChats(agentId: string | null): Conversation[] {
    const query = this.sidebarQuery.trim().toLowerCase();
    const agentMatches = this.agents.some(agent => agent.id === agentId && agent.name.toLowerCase().includes(query));
    return this.conversations.filter(chat => (chat.agentId || null) === agentId && (!query || agentMatches || chat.title.toLowerCase().includes(query)));
  }

  constructor(private readonly http: HttpClient) {}
  ngOnInit(): void {
    window.addEventListener('hashchange', this.onHashChange);
    this.onHashChange();
  }
  ngOnDestroy(): void { window.removeEventListener('hashchange', this.onHashChange); }

  signIn(): void {
    this.isSignedIn = true;
    window.history.replaceState(null, '', '#/');
    this.onHashChange();
    this.loadSpaces();
    this.refreshConversations();
  }

  logout(): void {
    this.isSignedIn = false;
    this.view = 'home';
    this.clearChat();
    this.initialEntityId = null;
    this.previewDocumentId = null;
    this.builderAgentId = null;
    this.sidebarOpen = false;
    this.sidebarCollapsed = false;
    this.sidebarQuery = '';
    this.showLegacy = false;
    this.expandedAgentIds.clear();
    this.agents = [];
    this.agentsLoaded = false;
    this.spaces = [];
    this.customers = [];
    this.conversations = [];
    window.history.replaceState(null, '', '#/login');
  }

  private onHashChange = (): void => {
    if (!this.isSignedIn) {
      window.history.replaceState(null, '', '#/login');
      return;
    }
    const [path, query] = window.location.hash.replace(/^#/, '').split('?');
    if (path === '/login') { this.logout(); return; }
    const params = new URLSearchParams(query || '');
    let match: RegExpExecArray | null;
    this.previewDocumentId = path === '/documents' ? this.previewDocumentId : null;
    this.initialEntityId = null;
    if ((match = /^\/agents\/([^/]+)\/chat(?:\/([a-f0-9-]+))?$/.exec(path))) {
      this.view = 'chat';
      this.activeAgentId = decodeURIComponent(match[1]);
      this.activeConversationId = match[2] || null;
      this.initialEntityId = params.get('customer');
      this.loadCustomers();
    } else if ((match = /^\/chat(?:\/([a-f0-9-]+))?$/.exec(path))) {
      this.view = 'chat'; this.activeAgentId = null; this.activeConversationId = match[1] || null;
    } else if (path === '/knowledge') { this.view = 'knowledge'; this.clearChat(); }
    else if (path === '/documents') { this.view = 'repository'; this.clearChat(); }
    else if ((match = /^\/builder(?:\/([^/]+))?$/.exec(path))) { this.view = 'builder'; this.builderAgentId = match[1] ? decodeURIComponent(match[1]) : null; this.clearChat(); }
    else { this.view = 'home'; this.clearChat(); this.loadAgents(); }
  };
  private clearChat(): void { this.activeAgentId = null; this.activeConversationId = null; }

  navigate(hash: string): void {
    if (window.location.hash === hash) this.onHashChange(); else window.location.hash = hash;
    this.sidebarOpen = false;
  }
  loadAgents(): void { this.http.get<Agent[]>(`${API_URL}/agents`).subscribe({ next: items => { this.agents = items; this.agentsLoaded = true; } }); }
  loadSpaces(): void { this.http.get<KnowledgeSpace[]>(`${API_URL}/knowledge/spaces`).subscribe({ next: items => this.spaces = items }); }
  loadCustomers(): void { this.http.get<Customer[]>(`${API_URL}/entities`).subscribe({ next: items => this.customers = items }); }
  refreshConversations(): void {
    this.loadCustomers();
    this.http.get<Conversation[]>(`${API_URL}/conversations?limit=100&offset=0`).subscribe({ next: items => this.conversations = items });
  }

  activeAgent(): Agent | null { return this.agents.find(item => item.id === this.activeAgentId) || null; }
  activeConversationTitle(): string | null { return this.conversations.find(item => item.id === this.activeConversationId)?.title || null; }
  chatsFor(agentId: string): Conversation[] { return this.conversations.filter(item => item.agentId === agentId); }
  legacyChats(): Conversation[] { return this.conversations.filter(item => !item.agentId); }
  spacesFor(agent: Agent): KnowledgeSpace[] { return this.spaces.filter(space => agent.spaceIds.includes(space.id)); }
  customerName(id: string | null): string | null { return id ? this.customers.find(item => item.id === id)?.name || 'Customer' : null; }
  initials(name: string): string { return name.split(/\s+/).filter(Boolean).slice(0, 2).map(word => word[0].toUpperCase()).join(''); }
  toggleAgent(id: string): void { this.expandedAgentIds.has(id) ? this.expandedAgentIds.delete(id) : this.expandedAgentIds.add(id); }

  newAgentChat(agentId: string): void { this.navigate(`#/agents/${encodeURIComponent(agentId)}/chat`); }
  startCustomerChat(agentId: string, entityId: string): void { this.navigate(`#/agents/${encodeURIComponent(agentId)}/chat?customer=${encodeURIComponent(entityId)}`); }
  openConversation(item: Conversation): void { this.navigate(item.agentId ? `#/agents/${encodeURIComponent(item.agentId)}/chat/${item.id}` : `#/chat/${item.id}`); }
  openRepository(): void { this.previewDocumentId = null; this.navigate('#/documents'); }
  openSource(documentId: string): void { this.previewDocumentId = documentId; this.navigate('#/documents'); }

  conversationChanged(conversation: Conversation): void {
    if (!this.isSignedIn) return;
    this.activeConversationId = conversation.id;
    const hash = conversation.agentId ? `#/agents/${encodeURIComponent(conversation.agentId)}/chat/${conversation.id}` : `#/chat/${conversation.id}`;
    if (window.location.hash !== hash) window.history.replaceState(null, '', hash);
    const isNew = !this.conversations.some(item => item.id === conversation.id);
    this.conversations = [conversation, ...this.conversations.filter(item => item.id !== conversation.id)];
    if (isNew) { this.loadAgents(); this.loadCustomers(); }
  }
  renameConversation(conversation: Conversation): void {
    const title = window.prompt('Conversation title', conversation.title)?.trim();
    if (!title || title === conversation.title) return;
    this.http.patch<Conversation>(`${API_URL}/conversations/${conversation.id}`, { title }).subscribe({
      next: updated => this.conversations = this.conversations.map(item => item.id === updated.id ? updated : item)
    });
  }
  deleteConversation(conversation: Conversation): void {
    if (!window.confirm(`Delete chat "${conversation.title}"? This cannot be undone.`)) return;
    this.http.delete(`${API_URL}/conversations/${conversation.id}`).subscribe({
      next: () => {
        this.conversations = this.conversations.filter(item => item.id !== conversation.id);
        if (this.activeConversationId === conversation.id) conversation.agentId ? this.newAgentChat(conversation.agentId) : this.navigate('#/');
      }
    });
  }
}

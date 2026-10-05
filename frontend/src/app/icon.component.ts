import { CommonModule } from '@angular/common';
import { Component, Input } from '@angular/core';

/** A single, consistent set of 24px outline icons for the workspace. */
@Component({
  selector: 'app-icon',
  standalone: true,
  imports: [CommonModule],
  template: `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
      <path *ngFor="let path of paths" [attr.d]="path"></path>
    </svg>
  `,
  styles: [':host { display: inline-flex; width: 20px; height: 20px; flex: 0 0 auto; vertical-align: middle; } svg { display: block; width: 100%; height: 100%; }']
})
export class IconComponent {
  @Input() name = 'file';
  private readonly icons: Record<string, string[]> = {
    home: ['m3 10 9-7 9 7v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z', 'M8 17h8'],
    file: ['M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z', 'M14 3v6h6M8 13h8M8 17h6M8 9h2'],
    folder: ['M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Zm0 1h18'],
    layers: ['m12 3 10 5-10 5L2 8Z', 'm2 12 10 5 10-5M2 16l10 5 10-5'],
    chat: ['M21 11.5a8.5 8.5 0 0 1-8.5 8.5 9 9 0 0 1-4-.9L3 21l1.9-5.5a9 9 0 0 1-.9-4A8.5 8.5 0 0 1 12.5 3a8.5 8.5 0 0 1 8.5 8.5Z'],
    users: ['M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M22 21v-2a4 4 0 0 0-3-3.87', 'M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM16 3.13a4 4 0 0 1 0 7.75'],
    user: ['M20 21v-2a6 6 0 0 0-6-6h-4a6 6 0 0 0-6 6v2', 'M16 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z'],
    logout: ['M9 4H4v16h5M9 12h12m-4-4 4 4-4 4'],
    eye: ['M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z', 'M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z'],
    'eye-off': ['m3 3 18 18M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a18 18 0 0 1-3 3.8M6.2 6.2A20 20 0 0 0 2 12s3.5 7 10 7a12 12 0 0 0 5.8-1.8M9.9 9.9a3 3 0 0 0 4.2 4.2'],
    agent: ['M16 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2M13.5 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z', 'M17 11h5m-2.5-2.5L22 11l-2.5 2.5'],
    workflow: ['M12 5v5M12 14v5M5 12h5M14 12h5', 'M14 3a2 2 0 1 1-4 0 2 2 0 0 1 4 0ZM14 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0ZM14 21a2 2 0 1 1-4 0 2 2 0 0 1 4 0ZM5 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0ZM23 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z'],
    search: ['M19 10.5a7.5 7.5 0 1 1-15 0 7.5 7.5 0 0 1 15 0ZM16 16l5 5'],
    plus: ['M12 5v14M5 12h14'],
    close: ['m6 6 12 12M6 18 18 6'],
    edit: ['m16 3 5 5M3 21l5-1L21 7a2.1 2.1 0 0 0-5-5L3 15Z'],
    trash: ['M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7'],
    chevron: ['m9 5 7 7-7 7'],
    down: ['m6 9 6 6 6-6'],
    panel: ['M9 3v18', 'M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z'],
    menu: ['M4 6h16M4 12h16M4 18h16'],
    upload: ['M7 17H6a4 4 0 0 1-.8-7.9 7 7 0 0 1 13.6-1.3A4.5 4.5 0 0 1 18 17h-1', 'M12 21V11m-4 4 4-4 4 4'],
    arrow: ['M5 12h14m-5-5 5 5-5 5'],
    image: ['M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z', 'm3 17 6-6 4 4 3-3 5 5M9 7h.01'],
    check: ['m6 12 4 4 8-8'],
    info: ['M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0ZM12 11v6M12 7h.01'],
    book: ['M12 5v16M12 5C9 3 5 3 2 4v16c3-1 7-1 10 1 3-2 7-2 10-1V4c-3-1-7-1-10 1Z'],
    checklist: ['M9 5h12M9 12h12M9 19h12M2 5l1 1 2-2M2 12l1 1 2-2M2 19l1 1 2-2'],
    more: ['M12 5h.01M12 12h.01M12 19h.01'],
    lock: ['M6 11h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1Z', 'M8 11V7a4 4 0 0 1 8 0v4'],
    shield: ['M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6Z', 'm9 12 2 2 4-4'],
    sparkle: ['M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6'],
    unlink: ['M9 15 15 9', 'M11 6l1-1a4 4 0 0 1 6 6l-1 1M13 18l-1 1a4 4 0 0 1-6-6l1-1', 'M3 3l18 18'],
    undo: ['M9 14 4 9l5-5', 'M4 9h11a5 5 0 0 1 0 10h-3']
  };
  get paths(): string[] { return this.icons[this.name] || this.icons['file']; }
}

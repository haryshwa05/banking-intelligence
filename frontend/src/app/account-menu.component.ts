import { CommonModule } from '@angular/common';
import { Component, ElementRef, EventEmitter, HostListener, Output, ViewChild } from '@angular/core';
import { IconComponent } from './icon.component';

@Component({
  selector: 'app-account-menu',
  standalone: true,
  imports: [CommonModule, IconComponent],
  template: `
    <div class="account-menu" (focusout)="onFocusOut($event)">
      <button #trigger type="button" class="account-avatar" aria-label="Account: Admin"
        [attr.aria-expanded]="open" aria-controls="account-dropdown" (click)="open = !open">
        <app-icon name="user"></app-icon>
      </button>
      <div *ngIf="open" id="account-dropdown" class="account-dropdown" aria-label="Account">
        <div class="account-details"><span class="account-name">Admin</span><span>Banking Intelligence</span></div>
        <button type="button" class="account-logout" (click)="open = false; loggedOut.emit()"><app-icon name="logout"></app-icon>Log out</button>
      </div>
    </div>
  `,
  styles: [`
    :host { display: block; flex: 0 0 auto; margin-left: auto; }
    .account-menu { position: relative; }
    .account-avatar { display: grid; width: 32px; height: 32px; place-items: center; padding: 0; border: 1px solid #e4e4e4; border-radius: 50%; color: #777; background: #f7f7f7; cursor: pointer; }
    .account-avatar app-icon { width: 17px; height: 17px; }
    .account-avatar:hover, .account-avatar[aria-expanded='true'] { border-color: #d5d5d5; color: #333; background: #eee; }
    .account-dropdown { position: absolute; z-index: 35; top: calc(100% + 10px); right: 0; width: 210px; padding: 5px; border: 1px solid #e7e7e7; border-radius: 10px; background: #fff; box-shadow: 0 6px 24px #0000000d, 0 2px 5px #00000004; }
    .account-details { display: grid; gap: 5px; margin-bottom: 4px; padding: 10px 10px 13px; border-bottom: 1px solid #efefef; color: #999; font-size: 11px; }
    .account-name { color: #333; font-size: 13px; }
    .account-logout { display: flex; width: 100%; align-items: center; gap: 9px; padding: 9px 10px; border: 0; border-radius: 6px; color: #666; background: transparent; font-size: 12px; cursor: pointer; text-align: left; }
    .account-logout app-icon { width: 16px; height: 16px; }
    .account-logout:hover { color: #222; background: #f5f5f5; }
  `]
})
export class AccountMenuComponent {
  @Output() loggedOut = new EventEmitter<void>();
  @ViewChild('trigger') trigger!: ElementRef<HTMLButtonElement>;
  open = false;

  constructor(private readonly element: ElementRef<HTMLElement>) {}

  @HostListener('document:click', ['$event'])
  onOutsideClick(event: MouseEvent): void {
    if (!this.element.nativeElement.contains(event.target as Node)) this.open = false;
  }

  @HostListener('document:keydown.escape')
  onEscape(): void {
    if (!this.open) return;
    this.open = false;
    this.trigger.nativeElement.focus();
  }

  onFocusOut(event: FocusEvent): void {
    if (event.relatedTarget && !this.element.nativeElement.contains(event.relatedTarget as Node)) this.open = false;
  }
}

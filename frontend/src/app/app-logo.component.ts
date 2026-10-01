import { Component } from '@angular/core';

@Component({
  selector: 'app-logo',
  standalone: true,
  template: `
    <svg viewBox="0 0 32 32" aria-hidden="true" focusable="false" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="m16 3 12 7v13l-12 7-12-7V10Z" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/>
      <path d="m4 10 12 7 12-7M16 17v13" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/>
      <path d="M16 17 28 10v13l-12 7Z" fill="currentColor"/>
    </svg>
  `,
  styles: [':host { display: inline-flex; } svg { display: block; width: 100%; height: 100%; }']
})
export class AppLogoComponent {}

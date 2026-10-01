import { Component } from '@angular/core';

@Component({
  selector: 'app-logo',
  standalone: true,
  template: `
    <svg viewBox="0 0 32 32" aria-hidden="true" focusable="false" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M9 4.5h11l4.5 4.5v18H9a2 2 0 0 1-2-2V6.5a2 2 0 0 1 2-2Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>
      <path d="M20 4.5V10h4.5M11.5 15h9M11.5 19h9M11.5 23h6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
  `
})
export class AppLogoComponent {}

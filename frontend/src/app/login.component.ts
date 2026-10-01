import { CommonModule } from '@angular/common';
import { AfterViewInit, Component, ElementRef, EventEmitter, Output, ViewChild } from '@angular/core';
import { AppLogoComponent } from './app-logo.component';
import { IconComponent } from './icon.component';

@Component({
  selector: 'app-login',
  standalone: true,
  imports: [CommonModule, AppLogoComponent, IconComponent],
  template: `
    <main class="login-page">
      <section class="login-story" aria-label="Banking Intelligence">
        <div class="login-brand"><app-logo></app-logo><span>Banking Intelligence</span></div>
        <div class="login-story-copy">
          <h2>Clarity for every<br>banking decision.</h2>
          <p>Bring your customer documents, policies, and AI agents together in one workspace.</p>
        </div>
      </section>

      <section class="login-form-panel" aria-labelledby="login-title">
        <div class="login-form-wrap">
          <div class="login-mobile-brand"><app-logo></app-logo><span>Banking Intelligence</span></div>
          <header class="login-heading">
            <h1 id="login-title">Sign in</h1>
            <p>Welcome back. Access your banking AI workspace.</p>
          </header>
          <form class="login-form" (submit)="$event.preventDefault(); signIn()">
            <div class="login-field">
              <label for="login-username">Username</label>
              <input #usernameInput id="login-username" name="username" type="text" autocomplete="username"
                autocapitalize="none" spellcheck="false" placeholder="Your username" required
                [value]="username" (input)="username = $any($event.target).value; error = ''"
                [attr.aria-invalid]="error ? 'true' : null" [attr.aria-describedby]="error ? 'login-error' : null">
            </div>
            <div class="login-field">
              <label for="login-password">Password</label>
              <div class="login-password-field">
                <input id="login-password" name="password" [type]="showPassword ? 'text' : 'password'"
                  autocomplete="current-password" placeholder="Your password" required
                  [value]="password" (input)="password = $any($event.target).value; error = ''"
                  [attr.aria-invalid]="error ? 'true' : null" [attr.aria-describedby]="error ? 'login-error' : null">
                <button type="button" class="password-visibility" (click)="showPassword = !showPassword"
                  [attr.aria-label]="showPassword ? 'Hide password' : 'Show password'" [attr.aria-pressed]="showPassword">
                  <app-icon [name]="showPassword ? 'eye-off' : 'eye'"></app-icon>
                </button>
              </div>
            </div>
            <p *ngIf="error" id="login-error" class="login-error" role="alert">{{ error }}</p>
            <button type="submit" class="login-submit">Sign in</button>
          </form>
          <p class="login-caption">Your documents. Your knowledge. One intelligent workspace.</p>
        </div>
      </section>
    </main>
  `,
  styleUrl: './login.component.css'
})
export class LoginComponent implements AfterViewInit {
  @Output() signedIn = new EventEmitter<void>();
  @ViewChild('usernameInput') usernameInput!: ElementRef<HTMLInputElement>;
  username = '';
  password = '';
  showPassword = false;
  error = '';

  ngAfterViewInit(): void { this.usernameInput.nativeElement.focus({ preventScroll: true }); }

  signIn(): void {
    // Local prototype entry screen only; no backend authentication or persistent session.
    if (this.username !== 'admin' || this.password !== 'admin') {
      this.error = 'The username or password is incorrect. Please try again.';
      return;
    }
    this.error = '';
    this.password = '';
    this.signedIn.emit();
  }
}

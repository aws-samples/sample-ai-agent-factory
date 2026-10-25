import { useCallback, useEffect, useRef, useState, type FocusEvent, type MouseEvent } from 'react';
import { flushSync } from 'react-dom';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { ChevronDown, Github, Hexagon, Menu, X } from 'lucide-react';
import { navigation, type NavItem } from '../content/data';
import { ISSUES_URL, REPO_URL, VULN_REPORT_URL } from '../content/links';
import { siteNav } from '../nav';
import { PATHS } from '../paths';
import { ExternalLink } from './ExternalLink';
import { HomeLink } from './HomeLink';
import { RouteChange } from './RouteChange';
import styles from './Layout.module.css';

/**
 * The Start section has a single entry in the main navigation, so the footer lists
 * its five pages here (same labels as the Start section chip nav).
 */
const START_LINKS: NavItem[] = [
  { path: PATHS.start, label: 'Get started' },
  { path: PATHS.whichProject, label: 'Which project fits?' },
  { path: PATHS.prerequisites, label: 'Prerequisites' },
  { path: PATHS.costsAndCleanup, label: 'Costs and cleanup' },
  { path: PATHS.faq, label: 'FAQ' },
];

/** Footer columns: one per top-level navigation entry, each with at least one link. */
const FOOTER_COLUMNS = navigation
  .map((item) => ({
    ...item,
    children: item.children && item.children.length > 0 ? item.children : item.path === PATHS.start ? START_LINKS : [],
  }))
  .filter((item) => item.children.length > 0);

const MOBILE_MENU_ID = 'mobile-menu';
const NEW_ISSUE_URL = `${ISSUES_URL}/new`;

export function Layout() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [openDropdown, setOpenDropdown] = useState<string | null>(null);
  const location = useLocation();
  const mobileMenuButtonRef = useRef<HTMLButtonElement>(null);
  const firstMobileLinkRef = useRef<HTMLAnchorElement>(null);
  const dropdownRefs = useRef(new Map<string, HTMLDivElement>());
  const dropdownButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  // True between a pointer-down inside an open dropdown and the matching pointer-up/click.
  const pointerInsideDropdown = useRef(false);

  // Close menus on route change
  useEffect(() => {
    setMobileMenuOpen(false);
    setOpenDropdown(null);
  }, [location.pathname]);

  // Escape closes menus and returns focus to the trigger
  useEffect(() => {
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (openDropdown) {
        const button = dropdownButtonRefs.current.get(openDropdown);
        setOpenDropdown(null);
        button?.focus();
      } else if (mobileMenuOpen) {
        setMobileMenuOpen(false);
        mobileMenuButtonRef.current?.focus();
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [mobileMenuOpen, openDropdown]);

  // Lock body scroll while the mobile menu is open
  useEffect(() => {
    document.body.style.overflow = mobileMenuOpen ? 'hidden' : '';
    return () => {
      document.body.style.overflow = '';
    };
  }, [mobileMenuOpen]);

  // Move focus into the mobile menu when it opens
  useEffect(() => {
    if (mobileMenuOpen) firstMobileLinkRef.current?.focus();
  }, [mobileMenuOpen]);

  // Close the open dropdown on pointer-down outside it
  useEffect(() => {
    if (!openDropdown) return;
    const handlePointerDown = (event: Event) => {
      const container = dropdownRefs.current.get(openDropdown);
      if (container && event.target instanceof Node && !container.contains(event.target)) {
        setOpenDropdown(null);
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [openDropdown]);

  const toggleDropdown = useCallback((id: string) => {
    setOpenDropdown((current) => (current === id ? null : id));
  }, []);

  // Close the dropdown when focus leaves it (Tab past the last item). WebKit does not
  // focus a link on pointer-down, so the trigger blurs with relatedTarget null before the
  // click reaches the link; ignore those blurs (outside pointer-downs close the menu anyway).
  const handleDropdownBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (pointerInsideDropdown.current) return;
    const next = event.relatedTarget as Node | null;
    if (next === null) return;
    if (!event.currentTarget.contains(next)) {
      setOpenDropdown(null);
    }
  };

  const markPointerInsideDropdown = () => {
    pointerInsideDropdown.current = true;
  };

  const clearPointerInsideDropdown = () => {
    pointerInsideDropdown.current = false;
  };

  const isActive = (match: string) => {
    if (match === '/') return location.pathname === '/';
    return location.pathname === match || location.pathname.startsWith(`${match}/`);
  };

  const isCurrent = (to: string) => location.pathname.replace(/\/$/, '') === to.replace(/\/$/, '');

  const setDropdownRef = useCallback((id: string, el: HTMLDivElement | null) => {
    if (el) dropdownRefs.current.set(id, el);
    else dropdownRefs.current.delete(id);
  }, []);

  const setDropdownButtonRef = useCallback((id: string, el: HTMLButtonElement | null) => {
    if (el) dropdownButtonRefs.current.set(id, el);
    else dropdownButtonRefs.current.delete(id);
  }, []);

  // Focus main, then scroll its top into view: focus() alone does not scroll when the tall
  // main already intersects the viewport (long doc page scrolled down), and the html
  // scroll-padding keeps the top clear of the sticky header. If the mobile menu is open,
  // close it synchronously first so main is no longer inert when it receives focus.
  const handleSkipLink = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (mobileMenuOpen) {
      flushSync(() => setMobileMenuOpen(false));
    }
    const main = document.getElementById('main-content');
    if (!main) return;
    main.focus({ preventScroll: true });
    main.scrollIntoView({ block: 'start' });
  };

  // Closing on link click (same event as the navigation) removes `inert` from main in the
  // same commit, so RouteChange can move focus to main instead of it falling back to body.
  const closeMobileMenu = () => setMobileMenuOpen(false);

  const closeMobileMenuAndRefocus = () => {
    setMobileMenuOpen(false);
    mobileMenuButtonRef.current?.focus();
  };

  const inertWhenMenuOpen = mobileMenuOpen ? { inert: '' as const } : {};

  return (
    <>
      <a href="#main-content" className="skip-link" onClick={handleSkipLink}>
        Skip to main content
      </a>

      <header className={styles.header}>
        <div className={styles.headerContent}>
          <HomeLink className={styles.logo}>
            <Hexagon size={24} className={styles.logoIcon} aria-hidden="true" />
            <span className={styles.logoText}>Agentic AI Factory</span>
            <span className="visually-hidden"> home</span>
          </HomeLink>

          <nav className={styles.desktopNav} aria-label="Main navigation">
            <ul className={styles.navList}>
              {siteNav.map((item) => {
                const id = item.label.toLowerCase();
                const menuId = `dropdown-${id}`;
                return (
                  <li key={item.label} className={styles.navItem}>
                    {item.children ? (
                      <div
                        className={styles.dropdown}
                        ref={(el) => setDropdownRef(id, el)}
                        onBlur={handleDropdownBlur}
                        onPointerDown={markPointerInsideDropdown}
                        onPointerUp={clearPointerInsideDropdown}
                        onPointerCancel={clearPointerInsideDropdown}
                        onClick={clearPointerInsideDropdown}
                      >
                        <button
                          type="button"
                          ref={(el) => setDropdownButtonRef(id, el)}
                          className={`${styles.navLink} ${isActive(item.match) ? styles.active : ''}`}
                          onClick={() => toggleDropdown(id)}
                          aria-expanded={openDropdown === id}
                          aria-controls={menuId}
                        >
                          {item.label}
                          <ChevronDown
                            size={16}
                            className={`${styles.chevron} ${openDropdown === id ? styles.chevronOpen : ''}`}
                            aria-hidden="true"
                          />
                        </button>
                        {openDropdown === id && (
                          <ul id={menuId} className={styles.dropdownMenu}>
                            {item.children.map((child) => (
                              <li key={child.to}>
                                <Link
                                  to={child.to}
                                  className={`${styles.dropdownLink} ${isCurrent(child.to) ? styles.active : ''}`}
                                  aria-current={isCurrent(child.to) ? 'page' : undefined}
                                >
                                  {child.label}
                                </Link>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    ) : (
                      <Link
                        to={item.to}
                        className={`${styles.navLink} ${isActive(item.match) ? styles.active : ''}`}
                        aria-current={isCurrent(item.to) ? 'page' : undefined}
                      >
                        {item.label}
                      </Link>
                    )}
                  </li>
                );
              })}
            </ul>
          </nav>

          <ExternalLink href={REPO_URL} className={styles.githubLink}>
            <Github size={20} aria-hidden="true" />
            <span className={styles.githubText}>GitHub</span>
          </ExternalLink>

          <button
            type="button"
            ref={mobileMenuButtonRef}
            className={styles.mobileMenuButton}
            onClick={() => setMobileMenuOpen((open) => !open)}
            aria-expanded={mobileMenuOpen}
            aria-controls={MOBILE_MENU_ID}
            aria-label={mobileMenuOpen ? 'Close menu' : 'Open menu'}
          >
            {mobileMenuOpen ? <X size={24} aria-hidden="true" /> : <Menu size={24} aria-hidden="true" />}
          </button>
        </div>

        {mobileMenuOpen && (
          <div
            id={MOBILE_MENU_ID}
            className={styles.mobileNav}
            role="dialog"
            aria-modal="true"
            aria-label="Site navigation"
          >
            <button type="button" className={styles.mobileClose} onClick={closeMobileMenuAndRefocus}>
              <X size={20} aria-hidden="true" />
              Close menu
            </button>
            <nav aria-label="Mobile navigation">
              <ul className={styles.mobileNavList}>
                {siteNav.map((item, index) =>
                  item.children ? (
                    <li key={item.label}>
                      <span className={styles.mobileGroupLabel} id={`mobile-group-${item.label.toLowerCase()}`}>
                        {item.label}
                      </span>
                      <ul className={styles.mobileSubmenu} aria-labelledby={`mobile-group-${item.label.toLowerCase()}`}>
                        {item.children.map((child, childIndex) => (
                          <li key={child.to}>
                            <Link
                              to={child.to}
                              ref={index === 0 && childIndex === 0 ? firstMobileLinkRef : undefined}
                              className={styles.mobileNavLink}
                              aria-current={isCurrent(child.to) ? 'page' : undefined}
                              onClick={closeMobileMenu}
                            >
                              {child.label}
                            </Link>
                          </li>
                        ))}
                      </ul>
                    </li>
                  ) : (
                    <li key={item.label}>
                      <Link
                        to={item.to}
                        ref={index === 0 ? firstMobileLinkRef : undefined}
                        className={styles.mobileNavLink}
                        aria-current={isCurrent(item.to) ? 'page' : undefined}
                        onClick={closeMobileMenu}
                      >
                        {item.label}
                      </Link>
                    </li>
                  ),
                )}
                <li>
                  <ExternalLink href={REPO_URL} className={styles.mobileNavLink} onClick={closeMobileMenu}>
                    GitHub
                  </ExternalLink>
                </li>
              </ul>
            </nav>
          </div>
        )}
      </header>

      <main id="main-content" tabIndex={-1} className={styles.main} {...inertWhenMenuOpen}>
        <RouteChange />
        <Outlet />
      </main>

      <footer className={`${styles.footer} on-dark`} {...inertWhenMenuOpen}>
        <div className={styles.footerContent}>
          <nav className={styles.footerNav} aria-label="Footer navigation">
            {FOOTER_COLUMNS.map((item) => (
              <div key={item.path} className={styles.footerColumn}>
                <h2 className={styles.footerHeading}>
                  <Link to={item.path} className={styles.footerHeadingLink}>
                    {item.label}
                  </Link>
                </h2>
                <ul className={styles.footerColumnLinks}>
                  {item.children.map((child) => (
                    <li key={child.path}>
                      <Link to={child.path} className={styles.footerNavLink}>
                        {child.label}
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </nav>
          <div className={styles.footerMeta}>
            <p className={styles.footerText}>
              This is sample code released under the MIT-0 License, not an AWS service. Review the architecture,
              security posture, and costs before you use it.
            </p>
            <ul className={styles.footerLinks} aria-label="Footer links">
              <li>
                <ExternalLink href={REPO_URL} className={styles.footerLink}>
                  Repository
                </ExternalLink>
              </li>
              <li>
                <Link to={PATHS.contributing} className={styles.footerLink}>
                  Contributing
                </Link>
              </li>
              <li>
                <ExternalLink href={NEW_ISSUE_URL} className={styles.footerLink}>
                  Report an issue
                </ExternalLink>
              </li>
              <li>
                <ExternalLink href={VULN_REPORT_URL} className={styles.footerLink}>
                  Security reporting
                </ExternalLink>
              </li>
            </ul>
            <p className={styles.buildStamp}>
              Built from <code className={styles.sha}>{__BUILD_SHA__}</code> on{' '}
              <time dateTime={__BUILD_DATE__}>{__BUILD_DATE__}</time>
            </p>
          </div>
        </div>
      </footer>
    </>
  );
}

import { useState, useEffect, useCallback, useRef } from 'react';
import { Outlet, Link, useLocation } from 'react-router-dom';
import { Menu, X, ChevronDown, ExternalLink, Github, Hexagon } from 'lucide-react';
import { navigation } from '../content/data';
import styles from './Layout.module.css';

export function Layout() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [openDropdown, setOpenDropdown] = useState<string | null>(null);
  const location = useLocation();
  const mobileMenuButtonRef = useRef<HTMLButtonElement>(null);
  const dropdownButtonRefs = useRef<Map<string, HTMLButtonElement>>(new Map());

  // Close mobile menu on route change
  useEffect(() => {
    setMobileMenuOpen(false);
    setOpenDropdown(null);
  }, [location.pathname]);

  // Handle escape key to close menus and return focus
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (openDropdown) {
          const btn = dropdownButtonRefs.current.get(openDropdown);
          setOpenDropdown(null);
          btn?.focus();
        } else if (mobileMenuOpen) {
          setMobileMenuOpen(false);
          mobileMenuButtonRef.current?.focus();
        }
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [mobileMenuOpen, openDropdown]);

  // Prevent body scroll when mobile menu is open
  useEffect(() => {
    if (mobileMenuOpen) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [mobileMenuOpen]);

  const toggleDropdown = useCallback((path: string) => {
    setOpenDropdown(prev => (prev === path ? null : path));
  }, []);

  const isActive = (path: string) => {
    if (path === '/') return location.pathname === '/';
    return location.pathname.startsWith(path);
  };

  const setDropdownButtonRef = useCallback((path: string, el: HTMLButtonElement | null) => {
    if (el) {
      dropdownButtonRefs.current.set(path, el);
    } else {
      dropdownButtonRefs.current.delete(path);
    }
  }, []);

  return (
    <>
      <a href="#main-content" className="skip-link">
        Skip to main content
      </a>

      <header className={styles.header}>
        <div className={styles.headerContent}>
          <Link to="/" className={styles.logo} aria-label="AI Agent Factory home">
            <Hexagon size={24} className={styles.logoIcon} aria-hidden="true" />
            <span className={styles.logoText}>AI Agent Factory</span>
          </Link>

          {/* Desktop navigation */}
          <nav className={styles.desktopNav} aria-label="Main navigation">
            <ul className={styles.navList}>
              {navigation.map((item) => (
                <li key={item.path} className={styles.navItem}>
                  {item.children ? (
                    <div className={styles.dropdown}>
                      <button
                        type="button"
                        ref={(el) => setDropdownButtonRef(item.path, el)}
                        className={`${styles.navLink} ${isActive(item.path) ? styles.active : ''}`}
                        onClick={() => toggleDropdown(item.path)}
                        aria-expanded={openDropdown === item.path}
                        aria-controls={`dropdown-${item.path.replace(/\//g, '-')}`}
                      >
                        {item.label}
                        <ChevronDown
                          size={16}
                          className={`${styles.chevron} ${openDropdown === item.path ? styles.chevronOpen : ''}`}
                          aria-hidden="true"
                        />
                      </button>
                      {openDropdown === item.path && (
                        <ul
                          id={`dropdown-${item.path.replace(/\//g, '-')}`}
                          className={styles.dropdownMenu}
                        >
                          {item.children.map((child) => (
                            <li key={child.path}>
                              <Link
                                to={child.path}
                                className={`${styles.dropdownLink} ${isActive(child.path) ? styles.active : ''}`}
                                aria-current={isActive(child.path) ? 'page' : undefined}
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
                      to={item.path}
                      className={`${styles.navLink} ${isActive(item.path) ? styles.active : ''}`}
                      aria-current={isActive(item.path) ? 'page' : undefined}
                    >
                      {item.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </nav>

          <a
            href="https://github.com/aws-samples/sample-ai-agent-factory"
            target="_blank"
            rel="noopener noreferrer"
            className={styles.githubLink}
            aria-label="View on GitHub (opens in new tab)"
          >
            <Github size={20} aria-hidden="true" />
            <span className={styles.githubText}>GitHub</span>
            <ExternalLink size={14} aria-hidden="true" />
          </a>

          {/* Mobile menu button */}
          <button
            type="button"
            ref={mobileMenuButtonRef}
            className={styles.mobileMenuButton}
            onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
            aria-expanded={mobileMenuOpen}
            aria-controls="mobile-menu"
            aria-label={mobileMenuOpen ? 'Close menu' : 'Open menu'}
          >
            {mobileMenuOpen ? <X size={24} aria-hidden="true" /> : <Menu size={24} aria-hidden="true" />}
          </button>
        </div>

        {/* Mobile navigation */}
        {mobileMenuOpen && (
          <nav
            id="mobile-menu"
            className={styles.mobileNav}
            aria-label="Mobile navigation"
          >
            <ul className={styles.mobileNavList}>
              {navigation.map((item) => (
                <li key={item.path}>
                  {item.children ? (
                    <>
                      <button
                        type="button"
                        className={styles.mobileNavButton}
                        onClick={() => toggleDropdown(item.path)}
                        aria-expanded={openDropdown === item.path}
                        aria-controls={`mobile-dropdown-${item.path.replace(/\//g, '-')}`}
                      >
                        {item.label}
                        <ChevronDown
                          size={20}
                          className={openDropdown === item.path ? styles.chevronOpen : ''}
                          aria-hidden="true"
                        />
                      </button>
                      {openDropdown === item.path && (
                        <ul
                          id={`mobile-dropdown-${item.path.replace(/\//g, '-')}`}
                          className={styles.mobileSubmenu}
                        >
                          {item.children.map((child) => (
                            <li key={child.path}>
                              <Link
                                to={child.path}
                                className={styles.mobileNavLink}
                                aria-current={isActive(child.path) ? 'page' : undefined}
                              >
                                {child.label}
                              </Link>
                            </li>
                          ))}
                        </ul>
                      )}
                    </>
                  ) : (
                    <Link
                      to={item.path}
                      className={styles.mobileNavLink}
                      aria-current={isActive(item.path) ? 'page' : undefined}
                    >
                      {item.label}
                    </Link>
                  )}
                </li>
              ))}
              <li>
                <a
                  href="https://github.com/aws-samples/sample-ai-agent-factory"
                  target="_blank"
                  rel="noopener noreferrer"
                  className={styles.mobileNavLink}
                >
                  GitHub <ExternalLink size={14} aria-hidden="true" />
                </a>
              </li>
            </ul>
          </nav>
        )}
      </header>

      <main id="main-content" className={styles.main}>
        <Outlet />
      </main>

      <footer className={styles.footer}>
        <div className={styles.footerContent}>
          <div className={styles.footerSection}>
            <p className={styles.footerText}>
              Sample code provided under MIT-0 License. See{' '}
              <a
                href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/LICENSE"
                target="_blank"
                rel="noopener noreferrer"
              >
                LICENSE
              </a>{' '}
              for details.
            </p>
            <p className={styles.footerNote}>
              This is sample code, not an AWS service. Review architecture, security, and costs before use.
            </p>
          </div>
          <div className={styles.footerLinks}>
            <a
              href="https://github.com/aws-samples/sample-ai-agent-factory"
              target="_blank"
              rel="noopener noreferrer"
            >
              Repository
            </a>
            <a
              href="https://github.com/aws-samples/sample-ai-agent-factory/blob/main/CONTRIBUTING.md"
              target="_blank"
              rel="noopener noreferrer"
            >
              Contributing
            </a>
            <a
              href="https://aws.amazon.com/bedrock/"
              target="_blank"
              rel="noopener noreferrer"
            >
              Amazon Bedrock
            </a>
          </div>
        </div>
      </footer>
    </>
  );
}

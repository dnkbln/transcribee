import { DefaultParams, Route, RouteProps, Router, Switch, useLocation } from 'wouter';

import { trimTrailingSlash } from './utils/trim_trailing_slash';
import { LoginPage } from './pages/login';
import { UserHomePage } from './pages/user_home';
import { NewDocumentPage } from './pages/new_document';
import { DocumentPage } from './pages/document';
import { PageNotFoundPage } from './pages/page_not_found';
import { ModalHolder } from './components/modal';
import { Helmet } from 'react-helmet';
import { registerCopyHandler } from './utils/copy_text';
import { useAuthData } from './utils/auth';
import { LoadingPage } from './pages/loading';
import { PagePage } from './pages/page';
import { AboutPage } from './pages/about';
import { useGetConfig } from './api/config';

registerCopyHandler();

export function AuthenticatedRoute<T extends DefaultParams = DefaultParams>({
  ...props
}: RouteProps<T>) {
  const [_, navigate] = useLocation();
  const { isLoggedIn, hasShareToken, isLoading } = useAuthData();
  const isAuthenticated = isLoggedIn || hasShareToken;
  if (isLoading) {
    return <Route component={LoadingPage} />;
  }
  if (!isAuthenticated) {
    navigate('/login');
    return null;
  }
  return <Route {...props} />;
}

export function LoggedInRoute<T extends DefaultParams = DefaultParams>({
  ...props
}: RouteProps<T>) {
  const [_, navigate] = useLocation();
  const { isLoggedIn, isLoading } = useAuthData();
  if (isLoading) {
    return <Route component={LoadingPage} />;
  }
  if (!isLoggedIn) {
    navigate('/login');
    return null;
  }
  return <Route {...props} />;
}

export function LoggedInRedirectRoute<T extends DefaultParams = DefaultParams>({
  ...props
}: RouteProps<T>) {
  const [_, navigate] = useLocation();
  const { isLoggedIn, isLoading } = useAuthData();
  const { data: config, isLoading: configLoading } = useGetConfig({});
  if (isLoading) {
    return <Route component={LoadingPage} />;
  }
  if (!isLoggedIn && !configLoading && config !== undefined) {
    if (config.logged_out_redirect_url) {
      window.location.replace(config.logged_out_redirect_url);
    } else {
      navigate('/login');
    }
    return null;
  }
  return <Route {...props} />;
}

export function App() {
  const routerBase = trimTrailingSlash(import.meta.env.BASE_URL);

  return (
    <Router base={routerBase}>
      <Helmet titleTemplate="%s | transcribee 🐝" defaultTitle="transcribee 🐝"></Helmet>
      <ModalHolder />
      <Switch>
        <Route path="/login" component={LoginPage} />
        <Route path="/page/:pageId" component={PagePage} />
        <Route path="/about" component={AboutPage} />

        <LoggedInRedirectRoute path="/" component={UserHomePage} />
        <LoggedInRoute path="/new" component={NewDocumentPage} />

        <AuthenticatedRoute path="/document/:documentId" component={DocumentPage} />

        <Route component={PageNotFoundPage} />
      </Switch>
    </Router>
  );
}

// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Project pages: the site is served from https://graysoncadams.github.io/ma-alexa-music-skill/
export default defineConfig({
	site: 'https://graysoncadams.github.io',
	base: '/ma-alexa-music-skill/',
	trailingSlash: 'always',
	integrations: [
		starlight({
			title: 'Music Assistant',
			description:
				'An Alexa Music Skill that plays a self-hosted Subsonic-compatible library on Echo devices.',
			logo: {
				src: './src/assets/logo.svg',
				alt: 'Music Assistant',
			},
			favicon: '/favicon.svg',
			customCss: ['./src/styles/theme.css'],
			social: [
				{
					icon: 'github',
					label: 'GitHub',
					href: 'https://github.com/GraysonCAdams/ma-alexa-music-skill',
				},
			],
			editLink: {
				baseUrl: 'https://github.com/GraysonCAdams/ma-alexa-music-skill/edit/main/docs/',
			},
			tableOfContents: { minHeadingLevel: 2, maxHeadingLevel: 3 },
			sidebar: [
				{
					label: 'Setup',
					items: [
						{ label: 'What this is', slug: 'setup/what-this-is' },
						{ label: 'Requirements', slug: 'setup/requirements' },
						{ label: 'Choosing an alias', slug: 'setup/alias' },
						{ label: 'Public HTTPS endpoint', slug: 'setup/ingress' },
						{ label: 'Deploy the bridge', slug: 'setup/deploy' },
						{ label: 'Reaching setup safely', slug: 'setup/admin-access' },
						{ label: 'Validate the endpoint', slug: 'setup/validate' },
						{ label: 'Create the skill', slug: 'setup/skill' },
						{ label: 'Catalog and enablement', slug: 'setup/catalog' },
						{ label: 'First play', slug: 'setup/first-play' },
						{ label: 'Troubleshooting', slug: 'setup/troubleshooting' },
					],
				},
				{
					label: 'Playing music',
					items: [
						{ label: 'Voice and text', slug: 'playing/voice-and-text' },
						{ label: 'Home Assistant', slug: 'playing/home-assistant' },
					],
				},
				{
					label: 'How it works',
					items: [
						{ label: 'Architecture', slug: 'how-it-works/architecture' },
						{ label: 'Queues and contentIds', slug: 'how-it-works/queues' },
						{ label: 'Directives', slug: 'how-it-works/directives' },
						{ label: 'Findings', slug: 'how-it-works/findings' },
					],
				},
				{
					label: 'Reference',
					items: [
						{ label: 'Configuration', slug: 'reference/configuration' },
						{ label: 'Routes', slug: 'reference/routes' },
						{ label: 'Gaps and limits', slug: 'reference/limits' },
					],
				},
			],
		}),
	],
});

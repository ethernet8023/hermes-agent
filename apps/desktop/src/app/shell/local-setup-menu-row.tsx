import { useStore } from '@nanostores/react'
import { useContext, useEffect } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { DropdownMenuSeparator } from '@/components/ui/dropdown-menu'
import { useI18n } from '@/i18n'
import { $localSetupRowFit, acceptLocalSetupOffer, refreshLocalSetupEligibility } from '@/store/local-setup-offer'

import { ModelMenuCloseContext } from './model-catalog-menu'

/**
 * "Run locally" at the top of the model menu, for as long as this machine
 * qualifies and nothing is set up. The permanent home of the local-setup
 * offer: the card can be dismissed, this row stays until setup completes.
 * Opening the menu is what reads eligibility, once per connection.
 */
export function LocalSetupMenuRow() {
  const fit = useStore($localSetupRowFit)
  const closeMenu = useContext(ModelMenuCloseContext)
  const copy = useI18n().t.shell.modelMenu.localSetup

  // Mounts once per menu open (a user action, not a poll): re-read so a finished setup retires the row.
  useEffect(() => {
    void refreshLocalSetupEligibility()
  }, [])

  if (!fit) {
    return null
  }

  return (
    <>
      <div
        className="m-1 flex items-center gap-2 rounded-md bg-accent/40 px-2 py-1.5"
        data-slot="model-menu-local-setup"
      >
        <Codicon className="shrink-0 text-(--ui-accent)" name="chip" size="0.8rem" />
        <div className="min-w-0 flex-1 text-[0.7rem] leading-snug">
          <div className="font-medium text-foreground">{copy.title}</div>
          <div className="truncate text-muted-foreground">
            {copy.text(fit.model.display_name, fit.model.size_label)}
          </div>
        </div>
        <Button
          className="h-6 shrink-0 rounded-md px-2 text-[0.68rem]"
          onClick={() => {
            acceptLocalSetupOffer()
            closeMenu()
            // Hash route, not useNavigate: this menu also mounts outside the app Router (plugin surfaces).
            window.location.hash = '#/settings?tab=providers&pview=local'
          }}
          type="button"
          variant="secondary"
        >
          {copy.action}
        </Button>
      </div>
      <DropdownMenuSeparator className="mx-0" />
    </>
  )
}

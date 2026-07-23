import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

export function PlaceholderPage({ title }: { title: string }) {
  return (
    <div className="p-6">
      <Card className="max-w-md">
        <CardHeader>
          <CardTitle className="font-heading">{title}</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Not yet migrated to the new console.{" "}
          <a href="/dashboard/legacy.html" className="text-primary underline underline-offset-4">
            Open the classic dashboard ↗
          </a>
        </CardContent>
      </Card>
    </div>
  )
}

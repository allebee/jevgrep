# Records docs/demo.gif with https://github.com/charmbracelet/vhs:
#   OPENROUTER_API_KEY=... vhs docs/demo.tape
Output docs/demo.gif
Set FontSize 15
Set Width 1200
Set Height 640
Set TypingSpeed 40ms

Type 'cat examples/sample.log | wc -l'
Enter
Sleep 1s
Type 'cat examples/sample.log | jevgrep "Mentions a payment failure" -n --score --stats | head -12'
Enter
Sleep 8s
Type 'jevgrep "This line reports a real error, not routine noise" examples/sample.log --explain > /dev/null'
Enter
Sleep 14s
